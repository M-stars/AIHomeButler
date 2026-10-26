import pytest

from butler.devices.base import (
    DeviceError,
    DeviceInfo,
    DeviceOffline,
    TYPE_FAN,
    TYPE_LIGHT,
    TYPE_SENSOR,
)
from butler.devices.registry import DeviceRegistry
from butler.devices.simulated import JsonStateStore, SimulatedFan, SimulatedLight, SimulatedSensor


def test_sensor_readings_have_freshness(store):
    sensor = SimulatedSensor(DeviceInfo("s1", "客厅传感器", TYPE_SENSOR, "客厅"), store)
    state = sensor.read_state()
    assert "temperature_c" in state and "humidity_percent" in state
    assert state["fresh"] is True
    assert "sampled_at" in state
    assert 10.0 <= state["temperature_c"] <= 35.0
    assert 20.0 <= state["humidity_percent"] <= 90.0


def test_sensor_offline_raises(store):
    sensor = SimulatedSensor(DeviceInfo("s1", "传感器", TYPE_SENSOR, "客厅"), store)
    sensor.set_unavailable(True)
    with pytest.raises(DeviceOffline):
        sensor.read_state()


def test_light_turn_on_off(store):
    light = SimulatedLight(DeviceInfo("l1", "客厅灯", TYPE_LIGHT, "客厅"), store)
    assert light.read_state()["on"] is False
    light.apply("turn_on")
    assert light.read_state()["on"] is True
    light.apply("turn_off")
    assert light.read_state()["on"] is False


def test_light_brightness_validation(store):
    light = SimulatedLight(DeviceInfo("l1", "灯", TYPE_LIGHT, "客厅"), store)
    with pytest.raises(DeviceError):
        light.apply("set_brightness", 0)
    with pytest.raises(DeviceError):
        light.apply("set_brightness", 101)
    with pytest.raises(DeviceError):
        light.apply("set_brightness", "abc")
    light.apply("set_brightness", 30)
    assert light.read_state()["brightness"] == 30


def test_light_unknown_action(store):
    light = SimulatedLight(DeviceInfo("l1", "灯", TYPE_LIGHT, "客厅"), store)
    with pytest.raises(DeviceError):
        light.apply("self_destruct")


def test_light_offline_apply_raises(store):
    light = SimulatedLight(DeviceInfo("l1", "灯", TYPE_LIGHT, "客厅"), store)
    light.set_unavailable(True)
    with pytest.raises(DeviceOffline):
        light.apply("turn_on")


def test_fan_speed_validation(store):
    fan = SimulatedFan(DeviceInfo("f1", "风扇", TYPE_FAN, "卧室"), store)
    fan.apply("set_speed", "high")
    assert fan.read_state()["speed"] == "high"
    with pytest.raises(DeviceError):
        fan.apply("set_speed", "turbo")
    with pytest.raises(DeviceError):
        fan.apply("set_speed", 3)


def test_state_persistence_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store1 = JsonStateStore(path)
    light = SimulatedLight(DeviceInfo("l1", "灯", TYPE_LIGHT, "客厅"), store1)
    light.apply("turn_on")
    # 用新的 store 与设备实例模拟进程重启
    store2 = JsonStateStore(path)
    light2 = SimulatedLight(DeviceInfo("l1", "灯", TYPE_LIGHT, "客厅"), store2)
    assert light2.read_state()["on"] is True


def test_corrupt_state_file_recovers(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{这不是JSON", encoding="utf-8")
    store = JsonStateStore(path)  # 不应抛异常
    light = SimulatedLight(DeviceInfo("l1", "灯", TYPE_LIGHT, "客厅"), store)
    assert light.read_state()["on"] is False
    assert path.with_suffix(".corrupt.json").exists()


def test_registry_unknown_device_raises():
    registry = DeviceRegistry()
    with pytest.raises(DeviceError) as excinfo:
        registry.get("no.such")
    assert "不存在" in str(excinfo.value)
