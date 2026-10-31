"""例行任务：预授权的定时设备动作（"早上 7 点自动开窗帘"级能力）。

与主动提醒（proactive.py，纯通知）的分工：
  - 提醒规则只说话不干活；例行任务在到点后真正执行设备动作。
安全设计（与全项目安全边界一致）：
  - 创建即授权：create_routine 为 MEDIUM 风险需用户确认，确认界面完整展示
    动作清单（时间 + 星期 + 每一步做什么）；创建时逐动作预检——设备必须在
    白名单注册表里、动作必须被该设备支持、值域预先校验，杜绝"创建了但
    永远失败"或"创建时才发现不支持"；
  - 执行时不再逐动作确认——这正是"预授权"的含义；作为补偿，全程审计日志 +
    失败立即通知（通知与审计共用 memory_store）；
  - 高风险设备类型（门锁/热水器/警报器等）在适配器层就不存在，结构上无法
    被写进任何例行任务；
  - 每天至多执行一次（last_fired_at 日期守卫，重启安全）；单任务动作数上限
    _MAX_ACTIONS；执行 fail-fast：某一步失败立即中止后续步骤并通知。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .devices.base import ControllableDevice, DeviceError, StateUnknownError
from .devices.registry import DeviceRegistry
from .memory.store import MemoryStore
from .proactive import schedule_due
from .tools.home import _action_took_effect

_log = logging.getLogger("butler.routines")

_MAX_ACTIONS = 10
_VALUE_FREE_ACTIONS = frozenset(
    {"turn_on", "turn_off", "open", "close", "stop", "start", "pause", "return_to_base"}
)


def _require_int_in_range(value, low: int, high: int, action: str) -> int:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError
        number = int(value)
    except (TypeError, ValueError):
        raise DeviceError(f"{action} 需要提供 {low}-{high} 的整数") from None
    if not low <= number <= high:
        raise DeviceError(f"{action} 需要提供 {low}-{high} 的整数")
    return number


def validate_step(registry: DeviceRegistry, step: Any) -> str:
    """创建前逐动作预检，返回可读的动作描述（如"客厅窗帘 set_position 40"）。"""
    if not isinstance(step, dict):
        raise DeviceError("动作必须是对象（含 device_id / action / 可选 value）")
    device_id = step.get("device_id")
    action = step.get("action")
    value = step.get("value")
    if not isinstance(device_id, str) or not device_id:
        raise DeviceError("动作缺少 device_id")
    if not isinstance(action, str) or not action:
        raise DeviceError("动作缺少 action")
    device = registry.get(device_id)  # 不存在 → DeviceNotFound
    if not isinstance(device, ControllableDevice):
        raise DeviceError(f"{device.info.name} 不是可控设备，不能进入例行任务")
    if action not in device.supported_actions():
        raise DeviceError(
            f"{device.info.name} 不支持动作 {action!r}，"
            f"支持：{', '.join(device.supported_actions())}"
        )
    if action == "set_brightness":
        _require_int_in_range(value, 1, 100, action)
    elif action in ("set_position", "set_humidity"):
        _require_int_in_range(value, 0, 100, action)
    elif action == "set_temperature":
        try:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError
            number = float(value)
        except (TypeError, ValueError):
            raise DeviceError("set_temperature 需要提供数字温度值") from None
        if not 5 <= number <= 100:
            raise DeviceError("set_temperature 超出合理范围（5-100）")
    elif action == "set_speed":
        if value not in ("low", "medium", "high"):
            raise DeviceError("set_speed 需要提供 low/medium/high")
    elif action in _VALUE_FREE_ACTIONS:
        if value is not None:
            raise DeviceError(f"{action} 不需要 value 参数")
    else:
        # 未纳入值域表的带值动作：拒绝并要求显式扩展（宁可误拒不可放行）
        raise DeviceError(f"动作 {action!r} 暂不支持进入例行任务")
    return f"{device.info.name} {action}" + (f" {value}" if value is not None else "")


class RoutineRunner:
    """到点执行预授权的例行任务。由后台调度器每轮调用 run_due()。"""

    def __init__(
        self,
        device_registry: DeviceRegistry,
        memory_store: MemoryStore,
        now_fn: Callable[[], datetime] = datetime.now,
        verify_fn: Callable[[str, Any, Dict], bool] = _action_took_effect,
        logger: logging.Logger = None,
    ) -> None:
        self._devices = device_registry
        self._store = memory_store
        self._now_fn = now_fn
        self._verify = verify_fn
        self._log = logger or _log

    def run_due(self) -> List[Dict[str, Any]]:
        """执行所有到点的启用例行任务，返回本轮执行结果（已落库）。"""
        now = self._now_fn()
        results: List[Dict[str, Any]] = []
        for routine in self._store.list_routines():
            if not routine["enabled"]:
                continue
            if not schedule_due(routine["schedule"], routine.get("last_fired_at"), now):
                continue
            results.append(self._execute(routine))
        return results

    def _execute(self, routine: Dict[str, Any]) -> Dict[str, Any]:
        name = routine["name"]
        actions: List[Dict[str, Any]] = routine.get("actions") or []
        # 落库时间戳与本运行的时钟源保持一致：守卫按 last_fired_at 日期排重，
        # 若这里走真实时钟而 now_fn 被注入（测试/模拟），日期错位会绕过每日一次守卫
        stamp = self._now_fn().astimezone().isoformat(timespec="seconds")
        self._log.info("例行任务 #%s(%s) 开始执行（%d 步）", routine["id"], name, len(actions))
        for index, step in enumerate(actions, start=1):
            try:
                summary = self._run_step(step)
            except DeviceError as exc:
                message = (
                    f"例行任务【{name}】在第 {index}/{len(actions)} 步失败，已中止后续步骤：{exc}"
                )
                self._store.mark_routine_fired(routine["id"], f"失败于第 {index} 步：{exc}", at=stamp)
                self._store.add_notification(
                    rule_id=None, rule_name=f"例行任务·{name}", message=message, level="warning"
                )
                self._store.add_audit(
                    action="routine_execute", detail=f"#{routine['id']} {name} 第{index}步", status="failed"
                )
                self._log.warning("例行任务 %s(%s) 第 %d 步失败：%s", routine["id"], name, index, exc)
                return {"routine_id": routine["id"], "name": name, "status": "failed", "detail": message}
            except StateUnknownError as exc:
                # 传输层故障（如 HA 宕机）：指令可能已提交但结果不明——绝不能重试
                # （P1 修复：此异常不是 DeviceError 子类，原先会逃逸导致任务未标记
                # 已执行，在补跑窗口内每轮重发控制指令且用户无感知）
                message = (
                    f"例行任务【{name}】第 {index}/{len(actions)} 步无法确认结果"
                    f"（{exc}），本轮到此为止，明天到点将按计划重新执行"
                )
                self._store.mark_routine_fired(routine["id"], f"第 {index} 步结果不明", at=stamp)
                self._store.add_notification(
                    rule_id=None, rule_name=f"例行任务·{name}", message=message, level="warning"
                )
                self._store.add_audit(
                    action="routine_execute",
                    detail=f"#{routine['id']} {name} 第{index}步结果不明",
                    status="unknown",
                )
                self._log.warning("例行任务 %s(%s) 第 %d 步结果不明：%s", routine["id"], name, index, exc)
                return {"routine_id": routine["id"], "name": name, "status": "unknown", "detail": message}
            self._store.add_audit(
                action="routine_execute",
                detail=f"#{routine['id']} {name} 第{index}/{len(actions)}步：{summary}",
                status="ok",
            )
        result = f"完成 {len(actions)} 步"
        self._store.mark_routine_fired(routine["id"], result, at=stamp)
        self._store.add_audit(
            action="routine_execute", detail=f"#{routine['id']} {name} 全部完成", status="ok"
        )
        if routine.get("notify_on_success"):
            self._store.add_notification(
                rule_id=None, rule_name=f"例行任务·{name}",
                message=f"例行任务【{name}】已执行完成（{len(actions)} 步）", level="info",
            )
        self._log.info("例行任务 %s(%s) 执行完成", routine["id"], name)
        return {"routine_id": routine["id"], "name": name, "status": "executed", "detail": result}

    def _run_step(self, step: Dict[str, Any]) -> str:
        device = self._devices.get(step["device_id"])
        action = step["action"]
        value = step.get("value")
        state_before = device.read_state()
        device.apply(action, value)
        try:
            state_after = device.read_state()
        except DeviceError as exc:
            raise DeviceError(f"回读状态失败：{exc}") from exc
        if not self._verify(action, value, state_after):
            raise DeviceError(f"执行后验证失败：{action} 执行后状态仍为 {state_after}")
        return f"{device.info.name} {action}" + (f" {value}" if value is not None else "")


def build_routine_tools(memory_store: MemoryStore, device_registry: DeviceRegistry) -> List:
    """例行任务管理工具。创建=预授权（确认界面展示完整动作清单）。"""
    from .tools.base import RISK_LOW, RISK_MEDIUM, ToolSpec
    from .proactive import normalize_days

    def _list_routines(args: Dict) -> Dict:
        routines = memory_store.list_routines()
        return {"routines": routines, "count": len(routines)}

    def _create_routine(args: Dict) -> Dict:
        actions = args.get("actions")
        if not isinstance(actions, list) or not actions:
            raise DeviceError("actions 需要至少一个动作")
        if len(actions) > _MAX_ACTIONS:
            raise DeviceError(f"单个例行任务最多 {_MAX_ACTIONS} 个动作")
        validated = [validate_step(device_registry, step) for step in actions]
        schedule = {
            "time": args["time"].strip(),
            "days": normalize_days(args.get("days", "daily")),
        }
        if args.get("catchup_minutes") is not None:
            schedule["catchup_minutes"] = args["catchup_minutes"]
        routine_id = memory_store.add_routine(
            name=args["name"],
            schedule=schedule,
            actions=actions,
            notify_on_success=bool(args.get("notify_on_success", False)),
        )
        return {"routine_id": routine_id, "created": True, "steps": validated}

    def _delete_routine(args: Dict) -> Dict:
        if not memory_store.delete_routine(args["routine_id"]):
            raise DeviceError(f"例行任务 #{args['routine_id']} 不存在")
        return {"routine_id": args["routine_id"], "deleted": True}

    def _set_routine_enabled(args: Dict) -> Dict:
        if not memory_store.set_routine_enabled(args["routine_id"], bool(args["enabled"])):
            raise DeviceError(f"例行任务 #{args['routine_id']} 不存在")
        return {"routine_id": args["routine_id"], "enabled": bool(args["enabled"])}

    schedule_schema = {
        "time": {"type": "string", "description": "24 小时制 HH:MM 本地时间，如 07:00"},
        "days": {
            "description": (
                "星期几：daily / weekdays / weekend，或 0-6 整数列表"
                "（0=周一…6=周日）；不传=每天"
            ),
        },
        "catchup_minutes": {
            "type": "integer", "minimum": 0, "maximum": 720,
            "description": "错过触发时刻的补跑窗口分钟数，默认 30",
        },
    }

    return [
        ToolSpec(
            name="list_routines",
            description="列出所有例行任务（预授权的定时设备动作）及其启停状态。",
            parameters={"type": "object", "properties": {}, "required": []},
            risk=RISK_LOW,
            handler=_list_routines,
            timeout_seconds=5.0,
            retryable=True,
        ),
        ToolSpec(
            name="create_routine",
            description=(
                "创建例行任务：到点后自动按顺序执行一批设备动作，无需再次确认"
                "（创建时的确认即授权）。例如：工作日 07:00 自动开客厅窗帘。"
                "动作只允许白名单设备的受支持操作；创建时会逐动作校验。需用户确认。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名，如：早安模式"},
                    **schedule_schema,
                    "actions": {
                        "type": "array",
                        "description": (
                            "按顺序执行的动作列表（最多 10 个），每项为 "
                            '{"device_id": "...", "action": "...", "value": 可选}；'
                            "动作与 control_device 相同"
                        ),
                    },
                    "notify_on_success": {
                        "type": "boolean",
                        "description": "执行成功后是否也发通知（默认 false，失败必通知）",
                    },
                },
                "required": ["name", "time", "actions"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_create_routine,
            timeout_seconds=10.0,
        ),
        ToolSpec(
            name="delete_routine",
            description="删除一个例行任务（同时撤销其预授权）。需用户确认。",
            parameters={
                "type": "object",
                "properties": {"routine_id": {"type": "integer"}},
                "required": ["routine_id"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_delete_routine,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="set_routine_enabled",
            description="启用或停用一个例行任务。需用户确认。",
            parameters={
                "type": "object",
                "properties": {
                    "routine_id": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["routine_id", "enabled"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_set_routine_enabled,
            timeout_seconds=5.0,
        ),
    ]
