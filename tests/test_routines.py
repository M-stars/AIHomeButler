"""例行任务测试：预授权定时动作、fail-fast 中止、每日一次守卫、工具校验、调度器挂钩。"""
import json
import time
from datetime import date, datetime

import pytest

from butler.confirmation import AutoApproveConfirmer
from butler.devices.base import ControllableDevice, DeviceError, DeviceInfo, TYPE_LIGHT
from butler.devices.registry import DeviceRegistry
from butler.memory.store import MemoryStore
from butler.routines import RoutineRunner, build_routine_tools, validate_step
from butler.tools.registry import ToolRegistry
from helpers import make_executor

# 2026-10-31 是周六；动态取 weekday 避免硬编码
_TARGET_DAY = date(2026, 10, 31).weekday()


class StubLight(ControllableDevice):
    """可控桩设备：记录调用序列，可指定哪些动作失败。"""

    def __init__(self, device_id="light.stub", name="测试灯", fail_actions=()):
        super().__init__(DeviceInfo(id=device_id, name=name, device_type=TYPE_LIGHT, area="客厅"))
        self.on = False
        self.calls = []
        self.fail_actions = set(fail_actions)

    def is_available(self) -> bool:
        return True

    def read_state(self):
        return {"on": self.on}

    def supported_actions(self):
        return ["turn_on", "turn_off", "set_brightness"]

    def apply(self, action, value=None):
        self.calls.append(action)
        if action in self.fail_actions:
            raise DeviceError(f"{self.info.name} 模拟执行失败")
        if action == "turn_on":
            self.on = True
        elif action == "turn_off":
            self.on = False
        # set_brightness：桩不记亮度，值域在创建时已校验


def _rig(tmp_path, *devices):
    registry = DeviceRegistry()
    for device in devices:
        registry.register(device)
    store = MemoryStore(tmp_path / "routines.db")
    runner = RoutineRunner(registry, store, now_fn=lambda: datetime(2026, 10, 31, 7, 0))
    return registry, store, runner


def test_routine_executes_in_order_and_runs_once_per_day(tmp_path):
    light = StubLight()
    registry, store, runner = _rig(tmp_path, light)
    rid = store.add_routine(
        "早安", {"time": "07:00", "days": [_TARGET_DAY]},
        [
            {"device_id": "light.stub", "action": "turn_on"},
            {"device_id": "light.stub", "action": "turn_off"},
        ],
    )
    results = runner.run_due()
    assert len(results) == 1 and results[0]["status"] == "executed"
    assert light.calls == ["turn_on", "turn_off"]
    stored = next(r for r in store.list_routines() if r["id"] == rid)
    assert stored["last_fired_at"] and "完成" in stored["last_result"]
    # 当天已执行 → 不重复；成功默认不通知
    assert runner.run_due() == []
    assert store.list_notifications() == []


def test_routine_failure_aborts_remaining_and_notifies(tmp_path):
    good = StubLight(device_id="light.a", name="A灯")
    bad = StubLight(device_id="light.b", name="B灯", fail_actions={"turn_off"})
    registry, store, runner = _rig(tmp_path, good, bad)
    store.add_routine(
        "会失败的例行", {"time": "07:00", "days": [_TARGET_DAY]},
        [
            {"device_id": "light.a", "action": "turn_on"},
            {"device_id": "light.b", "action": "turn_off"},  # 失败
            {"device_id": "light.a", "action": "turn_off"},  # 不应执行
        ],
    )
    results = runner.run_due()
    assert results[0]["status"] == "failed"
    assert bad.calls == ["turn_off"] and good.calls == ["turn_on"]
    notifications = store.list_notifications()
    assert any("失败" in n["message"] and n["level"] == "warning" for n in notifications)
    audits = store.list_audit()
    assert any(a["action"] == "routine_execute" and a["status"] == "failed" for a in audits)


def test_routine_skipped_when_not_due_or_disabled(tmp_path):
    light = StubLight()
    registry, store, runner = _rig(tmp_path, light)
    store.add_routine(
        "别的日子", {"time": "07:00", "days": [(_TARGET_DAY + 1) % 7]},
        [{"device_id": "light.stub", "action": "turn_on"}],
    )
    disabled_id = store.add_routine(
        "已停用", {"time": "07:00", "days": [_TARGET_DAY]},
        [{"device_id": "light.stub", "action": "turn_on"}],
    )
    store.set_routine_enabled(disabled_id, False)
    assert runner.run_due() == []
    assert light.calls == []


def test_routine_success_notification_when_enabled(tmp_path):
    light = StubLight()
    registry, store, runner = _rig(tmp_path, light)
    store.add_routine(
        "要通知的", {"time": "07:00", "days": [_TARGET_DAY]},
        [{"device_id": "light.stub", "action": "turn_on"}],
        notify_on_success=True,
    )
    runner.run_due()
    assert any("已执行完成" in n["message"] for n in store.list_notifications())


def test_validate_step_rejects_bad_input(tmp_path):
    registry, _store, _runner = _rig(tmp_path, StubLight())
    with pytest.raises(DeviceError):
        validate_step(registry, {"device_id": "light.nope", "action": "turn_on"})
    with pytest.raises(DeviceError):
        validate_step(registry, {"device_id": "light.stub", "action": "set_brightness"})
    with pytest.raises(DeviceError):
        validate_step(registry, {"device_id": "light.stub", "action": "turn_on", "value": 1})
    with pytest.raises(DeviceError):
        validate_step(registry, "not-a-dict")


def test_create_routine_tool_end_to_end(tmp_path):
    registry, store, _runner = _rig(tmp_path, StubLight())
    tools = ToolRegistry()
    for spec in build_routine_tools(store, registry):
        tools.register(spec)
    executor = make_executor(tools, AutoApproveConfirmer())

    result = executor.execute(
        "create_routine",
        json.dumps({
            "name": "早安", "time": "07:00", "days": "weekdays",
            "actions": [{"device_id": "light.stub", "action": "turn_on"}],
        }),
    )
    assert result.status == "ok"
    stored = store.list_routines()[0]
    assert stored["schedule"]["days"] == [0, 1, 2, 3, 4]  # weekdays 归一化
    assert stored["enabled"] is True

    # 值域非法在创建时被拒
    result = executor.execute(
        "create_routine",
        json.dumps({
            "name": "坏亮度", "time": "07:00",
            "actions": [{"device_id": "light.stub", "action": "set_brightness", "value": 200}],
        }),
    )
    assert result.status == "error" and "1-100" in result.message

    # 动作数超限被拒
    many = [{"device_id": "light.stub", "action": "turn_on"}] * 11
    result = executor.execute(
        "create_routine", json.dumps({"name": "太多", "time": "07:00", "actions": many})
    )
    assert result.status == "error" and "10" in result.message

    # 创建需确认：拒绝时不落库
    from butler.confirmation import AutoDenyConfirmer

    deny_executor = make_executor(tools, AutoDenyConfirmer())
    result = deny_executor.execute(
        "create_routine",
        json.dumps({
            "name": "被拒的", "time": "07:00",
            "actions": [{"device_id": "light.stub", "action": "turn_on"}],
        }),
    )
    assert result.status == "declined"
    assert len(store.list_routines()) == 1


def test_scheduler_invokes_extra_tick(tmp_path):
    """后台调度器每轮既跑规则检查，也跑例行任务执行器。"""
    from butler.scheduler import RuleScheduler

    class _Engine:
        def run_checks(self):
            return [{"id": 1, "message": "规则通知"}]

    seen = []
    scheduler = RuleScheduler(
        _Engine(),
        interval_seconds=0.05,
        min_interval_seconds=0.01,
        sink=seen.append,
        extra_tick=lambda: [{"id": 2, "message": "例行结果"}],
    )
    scheduler.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            messages = [n["message"] for n in seen]
            if "规则通知" in messages and "例行结果" in messages:
                break
            time.sleep(0.05)
        messages = [n["message"] for n in seen]
        assert "规则通知" in messages and "例行结果" in messages
    finally:
        scheduler.stop()
