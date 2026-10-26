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
