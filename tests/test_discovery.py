"""阶段 14.5 设备自动发现测试：分类/能力推导/排除策略/注册表换血/周期刷新。全部离线。"""
import threading

import pytest

from butler.devices.base import (
    TYPE_CLIMATE, TYPE_COVER, TYPE_FAN, TYPE_HUMIDIFIER, TYPE_LIGHT,
    TYPE_SENSOR, TYPE_SWITCH, TYPE_VACUUM,
)
from butler.devices.discovery import (
    DiscoveryRefresher,
    build_devices,
    classify_entities,
    discover_registry,
    load_exclusions,
)
from butler.devices.homeassistant import build_ha_registry, supported_actions_for
from butler.devices.registry import DeviceRegistry
from butler.memory.store import MemoryStore


class FakeHassClient:
    """离线 HA 客户端桩：返回预设 states，可注入故障。"""

    def __init__(self, states):
        self.states = states

    def get_states(self):
        if isinstance(self.states, Exception):
            raise self.states
        return self.states

    def get_state(self, entity_id):
        for s in self.states:
            if s["entity_id"] == entity_id:
                return s
        from butler.devices.base import DeviceNotFound
        raise DeviceNotFound(entity_id)

    def call_service(self, *args, **kwargs):  # 控制路径在本套测试中不触达
        raise AssertionError("测试不应触发服务调用")


def _state(entity_id, attributes=None, state="on"):
    return {"entity_id": entity_id, "state": state, "attributes": attributes or {}}


def _fixture_states():
    return [
        _state("light.bulb", {"supported_color_modes": ["brightness", "color_temp"],
                              "friendly_name": "卧室灯"}),
        _state("light.onoff_only", {"supported_color_modes": ["onoff"], "friendly_name": "走廊灯"}),
        _state("fan.standing", {"percentage": 66, "percentage_step": 33,
                                "supported_features": 1, "friendly_name": "落地扇"}),
        _state("switch.socket", {"friendly_name": "智能插座"}),
        _state("cover.curtain", {"current_position": 50, "device_class": "curtain",
                                 "supported_features": 15, "friendly_name": "客厅窗帘"}),
        _state("cover.garage", {"current_position": 0, "device_class": "garage_door",
                                "supported_features": 15}),
        _state("climate.ac", {"temperature": 24, "current_temperature": 22,
                              "hvac_modes": ["off", "cool"], "supported_features": 1,
                              "friendly_name": "卧室空调"}),
        _state("climate.heatcool", {"target_temp_high": 24, "target_temp_low": 20,
                                    "hvac_modes": ["off", "heat_cool"], "supported_features": 2}),
        _state("humidifier.bedroom", {"humidity": 50, "min_humidity": 30, "max_humidity": 80,
                                      "friendly_name": "加湿器"}),
        _state("vacuum.robot", {"battery_level": 88, "fan_speed": "medium",
                                "supported_features": 8192 + 4 + 8 + 16, "friendly_name": "扫地机"}),
        _state("sensor.temp", {"device_class": "temperature", "unit_of_measurement": "°C",
                               "state_class": "measurement"}, state="22.5"),
        _state("lock.front_door", {"friendly_name": "前门锁"}),
        _state("alarm_control_panel.home", {}),
        _state("water_heater.bath", {}),
        _state("siren.alarm", {}),
        _state("media_player.tv", {}),
    ]


# ---------------------------------------------------------------- 分类与安全排除

def test_classify_includes_controllable_and_sensors_skips_forbidden():
    specs, skipped = classify_entities(_fixture_states())
    by_id = {s.entity_id: s for s in specs}
    assert by_id["light.bulb"].device_type == TYPE_LIGHT
    assert by_id["fan.standing"].device_type == TYPE_FAN
    assert by_id["switch.socket"].device_type == TYPE_SWITCH
    assert by_id["cover.curtain"].device_type == TYPE_COVER
    assert by_id["climate.ac"].device_type == TYPE_CLIMATE
    assert by_id["humidifier.bedroom"].device_type == TYPE_HUMIDIFIER
    assert by_id["vacuum.robot"].device_type == TYPE_VACUUM
    assert by_id["sensor.temp"].device_type == TYPE_SENSOR
    # 高风险域 + 车库门 + 不可控域全部被跳过且原因可读
    skip_reasons = dict(skipped)
    assert "高风险域结构性排除" in skip_reasons["lock.front_door"]
    assert "高风险域结构性排除" in skip_reasons["alarm_control_panel.home"]
    assert "高风险域结构性排除" in skip_reasons["water_heater.bath"]
    assert "高风险域结构性排除" in skip_reasons["siren.alarm"]
    assert "车库/院门结构性排除" in skip_reasons["cover.garage"]
    assert "media_player.tv" in skip_reasons


def test_classify_honors_user_exclusions():
    specs, skipped = classify_entities(_fixture_states(), exclusions={"fan.standing"})
    assert "fan.standing" not in {s.entity_id for s in specs}
    assert dict(skipped)["fan.standing"] == "用户排除表"


def test_exclusion_file_missing_or_broken_is_empty_set(tmp_path):
    assert load_exclusions(tmp_path / "nope.json") == set()
    bad = tmp_path / "broken.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_exclusions(bad) == set()


# ---------------------------------------------------------------- 能力推导

def test_capability_light_brightness_vs_onoff():
    on = supported_actions_for("light", {"supported_color_modes": ["brightness", "color_temp"]})
    assert "set_brightness" in on
    off = supported_actions_for("light", {"supported_color_modes": ["onoff"]})
    assert off == ["turn_on", "turn_off"]


def test_capability_fan_requires_percentage_support():
    with_speed = supported_actions_for("fan", {"percentage": 66, "supported_features": 1})
    assert "set_speed" in with_speed
    plain = supported_actions_for("fan", {"preset_modes": ["low"]})
    assert plain == ["turn_on", "turn_off"]


def test_capability_cover_respects_feature_bits():
    full = supported_actions_for("cover", {"supported_features": 15})
    assert full == ["open", "close", "stop", "set_position"]
    partial = supported_actions_for("cover", {"supported_features": 1 | 2 | 8})  # 无 SET_POSITION
    assert "set_position" not in partial and "open" in partial
    assert supported_actions_for("cover", {"supported_features": 0}) is None  # 异常实体跳过


def test_capability_climate_double_setpoint_excluded():
    single = supported_actions_for("climate", {"temperature": 24, "supported_features": 1})
    assert "set_temperature" in single
    double = supported_actions_for(
        "climate", {"target_temp_high": 24, "target_temp_low": 20, "supported_features": 2}
    )
    assert double is None


def test_capability_vacuum_from_bits():
    actions = supported_actions_for(
        "vacuum", {"supported_features": 8192 | 4 | 8 | 16}
    )
    assert actions == ["start", "pause", "stop", "return_to_base"]
    minimal = supported_actions_for("vacuum", {"supported_features": 8192})
    assert minimal == ["start"]


def test_adapters_derive_actions_from_features_and_legacy_fallback():
    client = FakeHassClient([])
    specs, _ = classify_entities(_fixture_states())
    devices = {d.id: d for d in build_devices(client, specs)}
    assert devices["light.onoff_only"].supported_actions() == ["turn_on", "turn_off"]
    assert "set_brightness" in devices["light.bulb"].supported_actions()
    assert devices["cover.curtain"].supported_actions() == ["open", "close", "stop", "set_position"]
    assert devices["vacuum.robot"].supported_actions() == ["start", "pause", "stop", "return_to_base"]
    # legacy（features=None）保持旧白名单全集行为
    from butler.devices.base import DeviceInfo
    from butler.devices.homeassistant import HaLight, HaVacuum
    legacy_light = HaLight(client, DeviceInfo("light.x", "旧灯", TYPE_LIGHT, "客厅"))
    assert legacy_light.supported_actions() == ["turn_on", "turn_off", "set_brightness"]
    legacy_vacuum = HaVacuum(client, DeviceInfo("vacuum.x", "旧扫", TYPE_VACUUM, "客厅"))
    assert legacy_vacuum.supported_actions() == ["start", "pause", "stop", "return_to_base"]


# ---------------------------------------------------------------- 注册表与刷新

def test_discover_registry_builds_all_types():
    registry, specs, skipped = discover_registry(FakeHassClient(_fixture_states()))
    ids = {d.id for d in registry.list()}
    assert "light.bulb" in ids and "sensor.temp" in ids
    assert not any(e.startswith("lock.") or e.startswith("cover.garage") for e in ids)
    assert len(specs) == len(registry.list())
    assert skipped  # 有跳过记录可诊断


def test_registry_replace_all_atomic_and_rejects_empty():
    registry, _specs, _skipped = discover_registry(FakeHassClient(_fixture_states()))
    old_count = len(registry.list())
    with pytest.raises(Exception):
        registry.replace_all([])  # 空集拒换
    assert len(registry.list()) == old_count


def test_refresher_adds_removes_and_audits(tmp_path):
    states = _fixture_states()
    client = FakeHassClient(states)
    registry, _specs, _skipped = discover_registry(client)
    store = MemoryStore(tmp_path / "m.db")
    refresher = DiscoveryRefresher(client, registry, interval_seconds=60,
                                   memory_store=store, exclusions_path=tmp_path / "ex.json")

    # 新设备入网 → 刷新后自动纳入 + 审计
    states.append(_state("light.new_lamp", {"supported_color_modes": ["brightness"]}))
    result = refresher.refresh_once()
    assert result["added"] == ["light.new_lamp"] and result["removed"] == []
    assert "light.new_lamp" in {d.id for d in registry.list()}
    assert any(a["action"] == "discovery.found" and "light.new_lamp" in a["detail"]
               for a in store.list_audit())

    # 设备消失 → 自动移出 + 审计
    states[:] = [s for s in states if s["entity_id"] != "light.new_lamp"]
    result = refresher.refresh_once()
    assert result["removed"] == ["light.new_lamp"] and result["added"] == []
    assert "light.new_lamp" not in {d.id for d in registry.list()}

    # 无变化时不产生刷新审计噪音
    audits_before = len(store.list_audit())
    assert refresher.refresh_once() == {"added": [], "removed": []}
    assert len(store.list_audit()) == audits_before


def test_refresher_keeps_old_registry_when_ha_down(tmp_path):
    client = FakeHassClient(_fixture_states())
    registry, _specs, _skipped = discover_registry(client)
    before = {d.id for d in registry.list()}
    client.states = ConnectionError("HA 宕机")
    with pytest.raises(Exception):
        refresher = DiscoveryRefresher(client, registry, interval_seconds=60)
        refresher.refresh_once()
    assert {d.id for d in registry.list()} == before  # 绝不因刷新失败清空


def test_build_ha_registry_discovery_and_legacy_fallback(tmp_path):
    # 发现路径：FakeClient 注入，不需要网络
    cfg = type("C", (), {"discovery_enabled": True, "data_dir": tmp_path,
                         "ha_devices_file": tmp_path / "w.json"})()
    registry = build_ha_registry(cfg, client=FakeHassClient(_fixture_states()))
    assert "light.bulb" in {d.id for d in registry.list()}

    # legacy 回退：DISCOVERY_ENABLED=0 → 白名单文件
    whitelist = tmp_path / "w.json"
    whitelist.write_text(
        '{"devices": [{"type": "light", "entity_id": "light.legacy", "name": "旧灯", "area": "客厅"}]}',
        encoding="utf-8",
    )
    cfg2 = type("C", (), {"discovery_enabled": False, "data_dir": tmp_path,
                          "ha_devices_file": whitelist})()
    registry2 = build_ha_registry(cfg2, client=FakeHassClient([]))
    legacy = registry2.get("light.legacy")
    assert legacy.supported_actions() == ["turn_on", "turn_off", "set_brightness"]  # 旧全集行为


def test_registry_thread_safe_during_replace():
    """刷新换血期间并发读不崩溃（冒烟：锁生效）。"""
    registry, specs, _ = discover_registry(FakeHassClient(_fixture_states()))
    client = FakeHassClient(_fixture_states())
    errors = []

    def reader():
        for _ in range(200):
            try:
                registry.list()
                registry.get("light.bulb")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    def swapper():
        for _ in range(50):
            try:
                registry.replace_all(build_devices(client, specs))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)] + [
        threading.Thread(target=swapper) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
