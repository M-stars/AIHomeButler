"""阶段 7 主动服务测试：确定性规则、连击门槛、冷却防霸屏、规则管理工具。"""
import json

from butler.confirmation import AutoApproveConfirmer, AutoDenyConfirmer
from butler.devices.base import Device, DeviceInfo, TYPE_SENSOR
from butler.devices.base import DeviceOffline
from butler.memory.store import MemoryStore
from butler.proactive import RuleEngine, build_proactive_tools
from butler.tools.registry import ToolRegistry
from helpers import make_executor


import pytest


@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path / "proactive.db")


class StubSensor(Device):
    """固定读数的桩传感器，便于确定性断言。"""

    def __init__(self, device_id: str, name: str):
        super().__init__(DeviceInfo(id=device_id, name=name, device_type=TYPE_SENSOR, area="客厅"))
        self.value = 22.0
        self.unavailable = False

    def is_available(self) -> bool:
        return not self.unavailable

    def read_state(self):
        if self.unavailable:
            raise DeviceOffline(f"{self.info.name} 离线")
        return {"value": self.value, "unit": "°C", "fresh": True, "sampled_at": "2026-10-27T00:00:00+08:00"}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _make_engine(mem, sensor, clock):
    from butler.devices.registry import DeviceRegistry

    registry = DeviceRegistry()
    registry.register(sensor)
    return RuleEngine(registry, mem, clock=clock)


def test_threshold_requires_consecutive_hits(mem):
    sensor = StubSensor("sensor.stub", "测试传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="高温提醒", kind="sensor_threshold",
        params={"device_id": "sensor.stub", "metric": "value", "op": ">", "threshold": 30.0},
        cooldown_seconds=300,
    )

    sensor.value = 31.0
    assert engine.run_checks() == []  # 第 1 次命中：未达连续门槛，不触发
    fired = engine.run_checks()       # 第 2 次连续命中：触发
    assert len(fired) == 1
    assert "31.0" in fired[0]["message"]


def test_single_spike_resets_after_recovery(mem):
    sensor = StubSensor("sensor.stub", "测试传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="高温提醒", kind="sensor_threshold",
        params={"device_id": "sensor.stub", "metric": "value", "op": ">", "threshold": 30.0},
    )
    sensor.value = 32.0
    engine.run_checks()  # 连击 1
    sensor.value = 22.0  # 恢复正常：连击清零
    engine.run_checks()
    sensor.value = 33.0  # 再次单次异常：仍不应触发
    assert engine.run_checks() == []


def test_cooldown_prevents_notification_spam(mem):
    sensor = StubSensor("sensor.stub", "测试传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="高温提醒", kind="sensor_threshold",
        params={"device_id": "sensor.stub", "metric": "value", "op": ">", "threshold": 30.0},
        cooldown_seconds=300,
    )
    sensor.value = 35.0
    engine.run_checks()
    first = engine.run_checks()
    assert len(first) == 1
    # 冷却期内即使持续超阈值也不再通知
    clock.advance(60)
    assert engine.run_checks() == []
    # 冷却期结束后才再次通知
    clock.advance(300)
    assert len(engine.run_checks()) == 1


def test_offline_rule_fires_and_respects_cooldown(mem):
    sensor = StubSensor("sensor.stub", "测试传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="离线提醒", kind="device_offline",
        params={"device_id": "sensor.stub"}, cooldown_seconds=600,
    )
    assert engine.run_checks() == []  # 在线：不触发
    sensor.unavailable = True
    assert len(engine.run_checks()) == 1
    assert engine.run_checks() == []  # 冷却期内不重复
    clock.advance(600)
    assert len(engine.run_checks()) == 1  # 冷却后再次提醒


def test_disabled_rule_does_not_fire(mem):
    sensor = StubSensor("sensor.stub", "测试传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    rule_id = mem.add_rule(
        name="离线提醒", kind="device_offline", params={"device_id": "sensor.stub"}
    )
    sensor.unavailable = True
    mem.set_rule_enabled(rule_id, False)
    assert engine.run_checks() == []
    mem.set_rule_enabled(rule_id, True)
    assert len(engine.run_checks()) == 1


def test_threshold_sensor_offline_resets_streak(mem):
    sensor = StubSensor("sensor.stub", "测试传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="高温提醒", kind="sensor_threshold",
        params={"device_id": "sensor.stub", "metric": "value", "op": ">", "threshold": 30.0},
    )
    sensor.value = 31.0
    engine.run_checks()          # 连击 1
    sensor.unavailable = True    # 传感器离线
    engine.run_checks()
    sensor.unavailable = False   # 恢复后重新计数
    engine.run_checks()          # 仍只有 1 次连击
    sensor.value = 31.5
    fired = engine.run_checks()  # 第 2 次连续命中
    assert len(fired) == 1


def test_ha_style_sensor_threshold_fires(mem):
    """P0 回归：HA 后端 HaSensor 返回的键是 value（非 temperature_c），
    metric="temperature" 的阈值规则必须照样触发，不得静默失效。"""
    sensor = StubSensor("sensor.stub", "HA温度传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="高温提醒", kind="sensor_threshold",
        params={"device_id": "sensor.stub", "metric": "temperature", "op": ">", "threshold": 30.0},
        cooldown_seconds=300,
    )
    sensor.value = 31.0  # StubSensor.read_state 返回 {"value": ...}，与 HaSensor 相同
    assert engine.run_checks() == []  # 连击 1
    fired = engine.run_checks()       # 连击 2 → 触发
    assert len(fired) == 1
    assert "value=31.0" in fired[0]["message"]
    assert "°C" in fired[0]["message"]  # 单位来自 state["unit"]（HaSensor 提供）


def test_simulated_style_sensor_threshold_still_fires(mem):
    """P0 回归对照：模拟后端返回 temperature_c，metric="temperature" 同样命中。"""
    from butler.devices.base import Device, DeviceInfo, TYPE_SENSOR

    class SimStyleSensor(Device):
        def __init__(self):
            super().__init__(DeviceInfo("sensor.sim", "模拟传感器", TYPE_SENSOR, "客厅"))
            self.temperature_c = 22.0

        def is_available(self):
            return True

        def read_state(self):
            return {"temperature_c": self.temperature_c, "humidity_percent": 50.0,
                    "sampled_at": "2026-10-27T00:00:00+08:00", "fresh": True}

    sensor = SimStyleSensor()
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="高温提醒", kind="sensor_threshold",
        params={"device_id": "sensor.sim", "metric": "temperature", "op": ">", "threshold": 30.0},
    )
    sensor.temperature_c = 32.0
    engine.run_checks()
    fired = engine.run_checks()
    assert len(fired) == 1
    assert "temperature_c=32.0" in fired[0]["message"]


def test_rule_for_missing_device_never_spams(mem):
    """P1 回归：规则引用不存在的设备时，异常路径必须静默记日志，
    不得每个检查周期发一条通知（后台调度器 60s 一轮会变成轰炸）。"""
    sensor = StubSensor("sensor.stub", "测试传感器")
    clock = FakeClock()
    engine = _make_engine(mem, sensor, clock)
    mem.add_rule(
        name="幽灵设备离线", kind="device_offline",
        params={"device_id": "sensor.ghost"}, cooldown_seconds=300,
    )
    for _ in range(5):
        clock.advance(600)  # 远超冷却期
        assert engine.run_checks() == []


def test_create_threshold_rule_rejects_non_sensor_device(tmp_path, registry):
    """P1 回归：不能给灯创建温度阈值规则——那是一条静默永不触发的哑弹规则。"""
    from butler.memory.store import MemoryStore
    from butler.proactive import build_proactive_tools
    from butler.tools.base import ToolSpec  # noqa: F401

    store = MemoryStore(tmp_path / "memory.db")
    registry_tools = ToolRegistry()
    for spec in build_proactive_tools(store, device_registry=registry):
        registry_tools.register(spec)
    executor = make_executor(registry_tools, AutoApproveConfirmer())

    result = executor.execute(
        "create_threshold_rule",
        json.dumps({
            "name": "灯的温度", "device_id": "light.living_room",
            "metric": "temperature", "op": ">", "threshold": 30,
        }),
    )
    assert result.status == "error"
    assert "不是传感器" in result.message
    assert store.list_rules() == []  # 拒绝创建


def test_create_threshold_rule_rejects_missing_metric(tmp_path, registry):
    """传感器存在但不提供该指标（如要 humidity 却只有温度）→ 拒绝。"""
    from butler.memory.store import MemoryStore
    from butler.proactive import build_proactive_tools

    store = MemoryStore(tmp_path / "memory.db")
    registry_tools = ToolRegistry()
    for spec in build_proactive_tools(store, device_registry=registry):
        registry_tools.register(spec)
    executor = make_executor(registry_tools, AutoApproveConfirmer())

    result = executor.execute(
        "create_threshold_rule",
        json.dumps({
            "name": "客厅温度", "device_id": "sensor.living_room",
            "metric": "temperature", "op": ">", "threshold": 30,
        }),
    )
    assert result.status == "ok"  # 模拟传感器提供 temperature_c → 放行

    result = executor.execute(
        "create_offline_rule",
        json.dumps({"name": "查无此灯", "device_id": "light.nowhere"}),
    )
    assert result.status == "error"
    assert "不存在" in result.message


def test_rule_tools_require_confirmation(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    registry = ToolRegistry()
    for spec in build_proactive_tools(store):
        registry.register(spec)

    approve_executor = make_executor(registry, AutoApproveConfirmer())
    result = approve_executor.execute(
        "create_threshold_rule",
        json.dumps({
            "name": "客厅高温", "device_id": "sensor.x", "metric": "temperature",
            "op": ">", "threshold": 30,
        }),
    )
    assert result.status == "ok"
    assert len(store.list_rules()) == 1

    deny_executor = make_executor(registry, AutoDenyConfirmer())
    result = deny_executor.execute(
        "create_offline_rule",
        json.dumps({"name": "设备离线", "device_id": "light.x"}),
    )
    assert result.status == "declined"
    assert len(store.list_rules()) == 1  # 拒绝 → 未创建

    rule_id = store.list_rules()[0]["id"]
    result = approve_executor.execute(
        "set_rule_enabled", json.dumps({"rule_id": rule_id, "enabled": False})
    )
    assert result.status == "ok"
    assert store.list_rules()[0]["enabled"] is False

    result = approve_executor.execute("list_rules", "{}")
    assert result.status == "ok" and result.data["count"] == 1


# ---------------------------------------------------------------- 定时提醒（schedule）

from datetime import date, datetime


def _schedule_engine(mem, now_dt):
    """schedule 规则不需要设备；now_fn 注入固定时间做确定性断言。"""
    from butler.devices.registry import DeviceRegistry

    registry = DeviceRegistry()
    return RuleEngine(registry, mem, now_fn=lambda: now_dt)


# 2026-10-31 是周六；动态取 weekday 避免硬编码星期数
_TARGET_DAY = date(2026, 10, 31).weekday()


def test_schedule_rule_fires_once_per_day_and_across_restart(mem):
    mem.add_rule("早晨提醒", "schedule", {
        "time": "08:00", "days": [_TARGET_DAY, (_TARGET_DAY + 1) % 7], "message": "该起床了",
    })
    fired = _schedule_engine(mem, datetime(2026, 10, 31, 8, 0)).run_checks()
    assert len(fired) == 1
    assert "08:00" in fired[0]["message"] and "该起床了" in fired[0]["message"]
    # 同日重复检查不触发
    assert _schedule_engine(mem, datetime(2026, 10, 31, 8, 1)).run_checks() == []
    # 新引擎实例（模拟重启）同日也不触发：last_fired_at 已持久化
    assert _schedule_engine(mem, datetime(2026, 10, 31, 8, 5)).run_checks() == []
    # 次日同一时刻重新触发
    next_day = _schedule_engine(mem, datetime(2026, 11, 1, 8, 0)).run_checks()
    assert len(next_day) == 1


def test_schedule_rule_respects_days(mem):
    other_day = (_TARGET_DAY + 1) % 7
    mem.add_rule("别的日子", "schedule", {"time": "08:00", "days": [other_day]})
    assert _schedule_engine(mem, datetime(2026, 10, 31, 8, 0)).run_checks() == []


def test_schedule_rule_catchup_window(mem):
    """到点时管家没在跑：窗口内（默认 30 分钟）补跑，窗口外不补。"""
    mem.add_rule("补跑", "schedule", {"time": "08:00", "days": [_TARGET_DAY]})
    fired = _schedule_engine(mem, datetime(2026, 10, 31, 8, 10)).run_checks()
    assert len(fired) == 1  # 迟到 10 分钟仍在窗口内


def test_schedule_rule_outside_catchup_window(mem):
    mem.add_rule("迟到太久", "schedule", {"time": "08:00", "days": [_TARGET_DAY]})
    assert _schedule_engine(mem, datetime(2026, 10, 31, 8, 31)).run_checks() == []
    assert _schedule_engine(mem, datetime(2026, 10, 31, 7, 59)).run_checks() == []


def test_schedule_member_directed_message(mem):
    mem.add_rule("量血压", "schedule", {
        "time": "09:00", "days": [_TARGET_DAY], "member": "爷爷", "message": "该量血压了",
    })
    fired = _schedule_engine(mem, datetime(2026, 10, 31, 9, 0)).run_checks()
    assert len(fired) == 1
    assert "爷爷，" in fired[0]["message"] and "该量血压了" in fired[0]["message"]


def test_create_schedule_rule_tool(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    registry_tools = ToolRegistry()
    for spec in build_proactive_tools(store):
        registry_tools.register(spec)
    approve_executor = make_executor(registry_tools, AutoApproveConfirmer())

    result = approve_executor.execute(
        "create_schedule_rule",
        json.dumps({"name": "上学提醒", "time": "07:20", "days": [0, 1, 2, 3, 4],
                    "member": "小星", "message": "出门上学啦"}),
    )
    assert result.status == "ok"
    rule = store.list_rules()[0]
    assert rule["kind"] == "schedule"
    assert rule["params"]["days"] == [0, 1, 2, 3, 4]
    assert rule["params"]["member"] == "小星"

    result = approve_executor.execute(
        "create_schedule_rule",
        json.dumps({"name": "坏时间", "time": "25:00"}),
    )
    assert result.status == "error"
    assert "HH:MM" in result.message

    result = approve_executor.execute(
        "create_schedule_rule",
        json.dumps({"name": "坏星期", "time": "08:00", "days": [7]}),
    )
    assert result.status == "error"
    assert "days" in result.message
