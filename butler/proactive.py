"""主动服务（阶段 7）：确定性规则引擎 + 通知，绝不自动控制设备。

设计要点（对应需求 4.7 / 阶段 7 验收）：
  - 只实现"纯通知"级别的主动服务；"已授权自动化控制设备"留待 HA 自动化承担，
    本模块不提供任何设备控制动作（安全边界：规则引擎根本没有执行器句柄）；
  - 区分确定性规则与模型推断：阈值/离线判断全部是确定性比较，无 LLM 参与；
  - 防霸屏：每条规则独立冷却期（默认 300s）+ 阈值规则要求连续 N 次检查命中
    （默认 2 次），单次异常数据不会触发；所有检查路径（含异常路径）都受冷却约束；
  - 规则可查看、创建、启停（创建/启停需用户确认，MEDIUM 风险）；
    创建时校验设备存在且能提供对应数据，杜绝"静默永不触发"的规则。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .devices.base import DeviceError, DeviceNotFound, TYPE_SENSOR
from .devices.registry import DeviceRegistry
from .memory.store import MemoryStore

_log = logging.getLogger("butler.proactive")

_DEFAULT_CONSECUTIVE = 2
_DEFAULT_COOLDOWN_SECONDS = 300
# 每个逻辑指标对应多个候选状态键：模拟后端返回 temperature_c/humidity_percent，
# HA 适配器（HaSensor）返回通用 value+unit。按序取第一个数值型命中，
# 避免规则在某一后端下静默失效（P0 修复：原实现只认单一键名）。
_METRIC_CANDIDATE_KEYS = {
    "temperature": ("temperature_c", "value"),
    "humidity": ("humidity_percent", "value"),
    "value": ("value",),
}
_UNIT_FALLBACKS = {"temperature_c": "°C", "humidity_percent": "%"}
_OPERATORS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


class RuleEngine:
    def __init__(
        self,
        device_registry: DeviceRegistry,
        memory_store: MemoryStore,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger = None,
    ) -> None:
        self._devices = device_registry
        self._store = memory_store
        self._clock = clock
        # 规则内存态：连续命中计数（不落库，重启后重新累计是安全默认）
        self._streaks: Dict[int, int] = {}
        self._last_fire_monotonic: Dict[int, float] = {}
        # 调度器线程 / CLI 对话后检查 / Web 端点可能并发调用 run_checks，
        # 连击计数与冷却判断必须串行化，否则会重复通知或丢计数
        self._lock = threading.RLock()
        self._log = logger or _log

    def run_checks(self) -> List[Dict[str, Any]]:
        """检查所有启用规则，返回本轮新产生的通知（已入库）。"""
        with self._lock:
            fired: List[Dict[str, Any]] = []
            for rule in self._store.list_rules():
                if not rule["enabled"]:
                    continue
                try:
                    notification = self._evaluate(rule)
                except DeviceNotFound as exc:
                    # 规则引用了不存在的设备：属配置错误，记录日志而不是
                    # 每个检查周期发一条通知轰炸（P1 修复）
                    self._streaks[rule["id"]] = 0
                    self._log.warning(
                        "规则 %s(%s) 引用的设备不存在，请删除或修正该规则：%s",
                        rule["id"], rule["name"], exc,
                    )
                    continue
                except DeviceError as exc:
                    # 设备离线等：threshold 规则重置连击；offline 规则本身就是信号，
                    # 但同样必须受冷却约束，否则每 60s 轰炸一条重复通知（P1 修复）
                    if rule["kind"] == "sensor_threshold":
                        self._streaks[rule["id"]] = 0
                    if rule["kind"] == "device_offline" and self._cooldown_ok(rule):
                        notification = self._fire(
                            rule,
                            f"设备【{rule['params'].get('device_id')}】无法读取：{exc}",
                            level="warning",
                        )
                        if notification:
                            fired.append(notification)
                    continue
                except Exception:
                    self._log.exception("规则 %s(%s) 评估异常", rule["id"], rule["name"])
                    continue
                if notification:
                    fired.append(notification)
            return fired

    def _evaluate(self, rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if rule["kind"] == "sensor_threshold":
            return self._eval_threshold(rule)
        if rule["kind"] == "device_offline":
            return self._eval_offline(rule)
        return None

    def _eval_threshold(self, rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        params = rule["params"]
        device_id = params.get("device_id")
        candidate_keys = _METRIC_CANDIDATE_KEYS.get(params.get("metric"), ())
        op = _OPERATORS.get(params.get("op"))
        threshold = params.get("threshold")
        if not device_id or not candidate_keys or op is None or not isinstance(threshold, (int, float)):
            self._log.warning("规则 %s 参数不完整，跳过", rule["id"])
            return None

        device = self._devices.get(device_id)
        state = device.read_state()  # 离线抛 DeviceOffline → 由 run_checks 统一处理
        value = None
        matched_key = None
        for key in candidate_keys:
            candidate = state.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                value = float(candidate)
                matched_key = key
                break
        if value is None:
            # 后端没给出任何可用的数值（数据缺失≠数据超标），连击清零
            self._streaks[rule["id"]] = 0
            return None

        required = int(params.get("consecutive", _DEFAULT_CONSECUTIVE))
        if op(value, float(threshold)):
            self._streaks[rule["id"]] = self._streaks.get(rule["id"], 0) + 1
        else:
            self._streaks[rule["id"]] = 0
            return None

        if self._streaks[rule["id"]] < required:
            return None
        if not self._cooldown_ok(rule):
            return None

        unit = state.get("unit") or _UNIT_FALLBACKS.get(matched_key, "")
        return self._fire(
            rule,
            f"【{rule['name']}】{device.info.name} 当前 {matched_key}={value}{unit}，"
            f"已连续 {self._streaks[rule['id']]} 次满足条件（{params.get('op')} {threshold}）",
            level=params.get("level", "info"),
        )

    def _eval_offline(self, rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        device_id = rule["params"].get("device_id")
        if not device_id:
            return None
        device = self._devices.get(device_id)
        if device.is_available():
            return None
        if not self._cooldown_ok(rule):
            return None
        return self._fire(
            rule, f"【{rule['name']}】设备 {device.info.name}（{device_id}）处于离线状态", level="warning"
        )

    def _cooldown_ok(self, rule: Dict[str, Any]) -> bool:
        cooldown = float(rule.get("cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS))
        last = self._last_fire_monotonic.get(rule["id"])
        if last is not None and (self._clock() - last) < cooldown:
            return False
        return True

    def _fire(self, rule: Dict[str, Any], message: str, level: str) -> Optional[Dict[str, Any]]:
        notification_id = self._store.add_notification(
            rule_id=rule["id"], rule_name=rule["name"], message=message, level=level
        )
        self._last_fire_monotonic[rule["id"]] = self._clock()
        self._store.update_rule_state(rule["id"], {"streak": self._streaks.get(rule["id"], 0)}, True)
        self._log.info("主动通知 rule=%s(%s) %s", rule["id"], rule["name"], message)
        return {"id": notification_id, "rule_id": rule["id"], "message": message, "level": level}


def build_proactive_tools(
    memory_store: MemoryStore, device_registry: Optional[DeviceRegistry] = None
) -> List:
    """规则管理工具。创建/启停规则会引发后续主动通知，均为 MEDIUM 风险需确认。

    device_registry 提供时，创建规则前校验设备存在且能提供所需数据，
    从源头杜绝"创建成功但永不触发"的静默哑弹规则。
    """
    from .tools.base import RISK_LOW, RISK_MEDIUM, ToolSpec

    def _list_rules(args: Dict) -> Dict:
        return {"rules": memory_store.list_rules(), "count": len(memory_store.list_rules())}

    def _validate_device(args: Dict, need_sensor_data: Optional[str] = None):
        """校验规则引用的设备。need_sensor_data 给定时额外确认设备能提供该指标。"""
        if device_registry is None:
            return None
        device = device_registry.get(args["device_id"])  # 不存在 → DeviceNotFound → 报错
        if need_sensor_data is not None:
            if device.info.device_type != TYPE_SENSOR:
                raise DeviceError(
                    f"{device.info.name} 不是传感器（类型 {device.info.device_type}），"
                    "无法创建数据阈值规则"
                )
            if device.is_available():
                try:
                    state = device.read_state()
                except DeviceError:
                    return device  # 当下离线不算配置错误，规则仍可创建
                candidate_keys = _METRIC_CANDIDATE_KEYS.get(need_sensor_data, ())
                if not any(
                    isinstance(state.get(key), (int, float)) and not isinstance(state.get(key), bool)
                    for key in candidate_keys
                ):
                    raise DeviceError(
                        f"{device.info.name} 不提供 {need_sensor_data} 数据"
                        f"（当前状态键：{sorted(state)}），请检查 metric 取值"
                    )
        return device

    def _create_threshold_rule(args: Dict) -> Dict:
        _validate_device(args, need_sensor_data=args["metric"])
        rule_id = memory_store.add_rule(
            name=args["name"],
            kind="sensor_threshold",
            params={
                "device_id": args["device_id"],
                "metric": args["metric"],
                "op": args["op"],
                "threshold": args["threshold"],
                "consecutive": args.get("consecutive", _DEFAULT_CONSECUTIVE),
                "level": args.get("level", "info"),
            },
            cooldown_seconds=args.get("cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS),
        )
        return {"rule_id": rule_id, "created": True}

    def _create_offline_rule(args: Dict) -> Dict:
        _validate_device(args)
        rule_id = memory_store.add_rule(
            name=args["name"],
            kind="device_offline",
            params={"device_id": args["device_id"]},
            cooldown_seconds=args.get("cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS),
        )
        return {"rule_id": rule_id, "created": True}

    def _set_rule_enabled(args: Dict) -> Dict:
        updated = memory_store.set_rule_enabled(args["rule_id"], bool(args["enabled"]))
        if not updated:
            from .devices.base import DeviceError

            raise DeviceError(f"规则 #{args['rule_id']} 不存在")
        return {"rule_id": args["rule_id"], "enabled": bool(args["enabled"])}

    def _list_notifications(args: Dict) -> Dict:
        return {
            "notifications": memory_store.list_notifications(
                unread_only=bool(args.get("unread_only", False))
            )
        }

    threshold_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "规则名，如：客厅高温提醒"},
            "device_id": {"type": "string", "description": "传感器设备 ID"},
            "metric": {
                "type": "string",
                "enum": ["temperature", "humidity", "value"],
                "description": "temperature/humidity 用于温湿度传感器，value 用于通用数值传感器",
            },
            "op": {"type": "string", "enum": [">", "<", ">=", "<="]},
            "threshold": {"type": "number"},
            "consecutive": {"type": "integer", "minimum": 1, "maximum": 10,
                            "description": "连续命中多少次才通知，默认 2"},
            "cooldown_seconds": {"type": "integer", "minimum": 60, "maximum": 86400,
                                 "description": "同一规则两次通知的最小间隔，默认 300"},
            "level": {"type": "string", "enum": ["info", "warning"]},
        },
        "required": ["name", "device_id", "metric", "op", "threshold"],
        "additionalProperties": False,
    }

    return [
        ToolSpec(
            name="list_rules",
            description="列出所有主动服务规则及其启停状态。",
            parameters={"type": "object", "properties": {}, "required": []},
            risk=RISK_LOW,
            handler=_list_rules,
            timeout_seconds=5.0,
            retryable=True,
        ),
        ToolSpec(
            name="create_threshold_rule",
            description=(
                "创建环境阈值提醒规则（纯通知，不会自动控制任何设备）。"
                "例如：客厅温度连续 2 次检查高于 30 度时提醒。"
                "目标必须是能提供对应数据的传感器，创建前会校验。需用户确认。"
            ),
            parameters=threshold_schema,
            risk=RISK_MEDIUM,
            handler=_create_threshold_rule,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="create_offline_rule",
            description="创建设备离线提醒规则（纯通知）。需用户确认。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "device_id": {"type": "string"},
                    "cooldown_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
                },
                "required": ["name", "device_id"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_create_offline_rule,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="set_rule_enabled",
            description="启用或停用一条主动服务规则。需用户确认。",
            parameters={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["rule_id", "enabled"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_set_rule_enabled,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="list_notifications",
            description="查看主动服务产生的通知（可只看未读）。",
            parameters={
                "type": "object",
                "properties": {
                    "unread_only": {"type": "boolean", "description": "默认 false"}
                },
                "required": [],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_list_notifications,
            timeout_seconds=5.0,
            retryable=True,
        ),
    ]
