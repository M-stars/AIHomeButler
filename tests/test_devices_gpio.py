"""GPIO 后端单元测试：注入假引脚工厂，不碰真实 GPIO（任意平台可跑）。

覆盖：灯（开关/亮度/PWM 校验）、风扇（三档/占空比）、继电器开关、
DHT 重试语义、DS18B20 sysfs 解析、PIR、清单文件解析与装配 fail-fast。
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from butler.config import ConfigError, load_config
from butler.devices.base import (
    TYPE_FAN,
    TYPE_LIGHT,
    TYPE_SENSOR,
    TYPE_SWITCH,
    DeviceError,
    DeviceInfo,
    DeviceOffline,
)
from butler.devices.gpio import (
    GpioDs18b20Sensor,
    GpioDhtSensor,
    GpioFan,
    GpioLight,
    GpioMotionSensor,
    GpioSwitch,
    GpiozeroFactory,
    build_gpio_registry,
    load_gpio_devices_file,
)

VALID_CONFIG = {
    "devices": [
        {"id": "light.desk_lamp", "name": "书桌台灯", "type": "light", "area": "书房",
         "pin": 17, "pwm": False},
        {"id": "light.strip", "name": "灯带", "type": "light", "area": "客厅",
         "pin": 18, "pwm": True},
        {"id": "fan.bench", "name": "测试风扇", "type": "fan", "area": "书房", "pin": 22},
        {"id": "switch.relay_1", "name": "继电器1", "type": "switch", "area": "书房",
         "pin": 23, "active_high": False},
        {"id": "sensor.climate", "name": "室温湿度", "type": "sensor", "area": "书房",
         "kind": "dht22", "pin": 4},
        {"id": "sensor.temp_probe", "name": "水温探针", "type": "sensor", "area": "书房",
         "kind": "ds18b20"},
        {"id": "sensor.motion", "name": "人体感应", "type": "sensor", "area": "书房",
         "kind": "motion", "pin": 27},
    ]
}


# ---------------------------------------------------------------- 假硬件
class FakeOutput:
    """gpiozero LED/PWMLED 的假实现：记录引脚参数，value 可读写。"""

    def __init__(self, pin, pwm=False, active_high=True):
        self.pin = pin
        self.pwm = pwm
        self.active_high = active_high
        self.value = 0.0

    def on(self):
        self.value = 1.0

    def off(self):
        self.value = 0.0


class FakeMotion:
    def __init__(self, pin):
        self.pin = pin
        self.value = 0.0


class FakeDht:
    """adafruit_dht 假实现：temperature/humidity 读数可注入失败。"""

    def __init__(self, pin, model, fail_times=0):
        self.pin = pin
        self.model = model
        self._fail_remaining = fail_times
        self.temperature = 23.6
        self.humidity = 52.4

    @property
    def temperature(self):
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("Checksum failed")
        return self._temperature

    @temperature.setter
    def temperature(self, value):
        self._temperature = value


class FakeFactory:
    """记录构建参数的假引脚工厂（对应真实 GpiozeroFactory 的接口）。"""

    def __init__(self):
        self.outputs = []
        self.motions = []
        self.dhts = []

    def make_output(self, pin, *, pwm, active_high):
        out = FakeOutput(pin, pwm=pwm, active_high=active_high)
        self.outputs.append(out)
        return out

    def make_motion(self, pin):
        m = FakeMotion(pin)
        self.motions.append(m)
        return m

    def make_dht(self, pin, model):
        d = FakeDht(pin, model)
        self.dhts.append(d)
        return d


def _info(device_id, device_type, name="测试设备"):
    return DeviceInfo(id=device_id, name=name, device_type=device_type, area="书房")


# ---------------------------------------------------------------- 灯
def test_light_on_off_and_default_brightness():
    device = GpioLight(_info("light.a", TYPE_LIGHT), FakeOutput(17, pwm=True), pwm=True)
    assert device.supported_actions() == ["turn_on", "turn_off", "set_brightness"]
    state = device.apply("turn_on")
    assert state == {"on": True, "brightness": 80}
    assert device.read_state() == {"on": True, "brightness": 80}
    # 与模拟后端语义一致：关灯后亮度仍报告记忆值
    assert device.apply("turn_off") == {"on": False, "brightness": 80}


def test_light_pwm_brightness_roundtrip():
    output = FakeOutput(18, pwm=True)
    device = GpioLight(_info("light.b", TYPE_LIGHT), output, pwm=True)
    device.apply("turn_on")
    state = device.apply("set_brightness", 35)
    assert state["brightness"] == 35
    assert output.value == pytest.approx(0.35)
    # 关灯状态下 set_brightness 只记忆档位，不点亮（与模拟后端语义一致）
    device.apply("turn_off")
    state = device.apply("set_brightness", 60)
    assert state == {"on": False, "brightness": 60}
    assert output.value == 0.0


def test_light_non_pwm_rejects_brightness():
    output = FakeOutput(17, pwm=False)
    device = GpioLight(_info("light.c", TYPE_LIGHT), output, pwm=False)
    assert device.supported_actions() == ["turn_on", "turn_off"]
    with pytest.raises(DeviceError, match="PWM"):
        device.apply("set_brightness", 50)
    with pytest.raises(DeviceError, match="PWM"):
        device.apply("turn_on", 50)
    device.apply("turn_on")  # 无亮度参数的开灯正常
    assert device.read_state() == {"on": True, "brightness": 80}


def test_light_brightness_validation():
    device = GpioLight(_info("light.d", TYPE_LIGHT), FakeOutput(17, pwm=True))
    for bad in (0, 101, "abc", True, None):
        with pytest.raises(DeviceError):
            device.apply("set_brightness", bad)


def test_light_write_failure_reports_offline():
    class BrokenOutput(FakeOutput):
        def on(self):
            raise OSError("pin busy")

    device = GpioLight(_info("light.e", TYPE_LIGHT), BrokenOutput(17, pwm=True))
    with pytest.raises(DeviceOffline, match="写引脚失败"):
        device.apply("turn_on")


# ---------------------------------------------------------------- 风扇
def test_fan_speeds_map_to_duty():
    output = FakeOutput(22, pwm=True)
    device = GpioFan(_info("fan.a", TYPE_FAN), output)
    state = device.apply("turn_on", "high")
    assert state == {"on": True, "speed": "high"}
    assert output.value == pytest.approx(1.0)
    assert device.apply("set_speed", "low")["speed"] == "low"
    assert output.value == pytest.approx(0.34)
    assert device.apply("turn_off") == {"on": False, "speed": "low"}
    assert output.value == 0.0


def test_fan_set_speed_does_not_turn_on_when_off():
    output = FakeOutput(22, pwm=True)
    device = GpioFan(_info("fan.b", TYPE_FAN), output)
    state = device.apply("set_speed", "high")  # 关机状态只记档位（与模拟后端一致）
    assert state == {"on": False, "speed": "high"}
    assert output.value == 0.0


def test_fan_invalid_speed_and_duty():
    with pytest.raises(DeviceError, match="档位"):
        GpioFan(_info("fan.c", TYPE_FAN), FakeOutput(22),
                speed_duty={"turbo": 0.5})
    with pytest.raises(DeviceError, match="占空比"):
        GpioFan(_info("fan.d", TYPE_FAN), FakeOutput(22), speed_duty={"low": 1.5})
    device = GpioFan(_info("fan.e", TYPE_FAN), FakeOutput(22))
    with pytest.raises(DeviceError, match="档位"):
        device.apply("set_speed", "turbo")


# ---------------------------------------------------------------- 开关
def test_switch_on_off():
    output = FakeOutput(23, pwm=False, active_high=False)
    device = GpioSwitch(_info("sw.a", TYPE_SWITCH), output)
    assert device.apply("turn_on") == {"on": True}
    assert output.value == 1.0
    assert device.apply("turn_off") == {"on": False}
    assert output.value == 0.0
    with pytest.raises(DeviceError):
        device.apply("set_brightness", 10)


# ---------------------------------------------------------------- DHT
def test_dht_read_success():
    device = GpioDhtSensor(_info("sensor.a", TYPE_SENSOR), lambda: FakeDht(4, "dht22"))
    assert device.is_available() is True
    state = device.read_state()
    assert state["temperature_c"] == 23.6
    assert state["humidity_percent"] == 52.4
    assert state["fresh"] is True
    assert state["sampled_at"]


def test_dht_retries_then_succeeds():
    device = GpioDhtSensor(
        _info("sensor.b", TYPE_SENSOR), lambda: FakeDht(4, "dht11", fail_times=1),
        retries=3, retry_seconds=0.0,
    )
    state = device.read_state()
    assert state["temperature_c"] == 23.6


def test_dht_retries_exhausted_reports_offline():
    device = GpioDhtSensor(
        _info("sensor.c", TYPE_SENSOR), lambda: FakeDht(4, "dht22", fail_times=99),
        retries=2, retry_seconds=0.0,
    )
    with pytest.raises(DeviceOffline, match="连续 2 次读取失败"):
        device.read_state()
    assert device.is_available() is True  # 硬件仍在，只是读不到


def test_dht_missing_dependency_registers_but_offline():
    def broken_reader():
        raise DeviceOffline("读取 dht22 需要依赖：pip install adafruit-circuitpython-dht")

    device = GpioDhtSensor(_info("sensor.d", TYPE_SENSOR), broken_reader)
    assert device.is_available() is False
    with pytest.raises(DeviceOffline, match="adafruit-circuitpython-dht"):
        device.read_state()


# ---------------------------------------------------------------- DS18B20
def _make_w1(tmp_path: Path, serial="28-0123456789ab", *, temperature=None, legacy=None):
    slave = tmp_path / serial
    slave.mkdir(parents=True)
    if temperature is not None:
        (slave / "temperature").write_text(temperature, encoding="ascii")
    if legacy is not None:
        (slave / "w1_slave").write_text(legacy, encoding="ascii")
    return tmp_path


def test_ds18b20_reads_temperature_file(tmp_path):
    base = _make_w1(tmp_path, temperature="23500")
    device = GpioDs18b20Sensor(_info("sensor.e", TYPE_SENSOR), base_dir=str(base))
    state = device.read_state()
    assert state["temperature_c"] == 23.5
    assert state["fresh"] is True


def test_ds18b20_reads_legacy_w1_slave(tmp_path):
    legacy = (
        "79 01 4b 37 75 00 00 00 7f : crc=79 YES\n"
        "79 01 4b 37 75 00 00 00 7f t=23437\n"
    )
    base = _make_w1(tmp_path, legacy=legacy)
    device = GpioDs18b20Sensor(_info("sensor.f", TYPE_SENSOR), base_dir=str(base))
    assert device.read_state()["temperature_c"] == 23.4


def test_ds18b20_crc_failure(tmp_path):
    legacy = (
        "79 01 4b 37 75 00 00 00 7f : crc=79 NO\n"
        "79 01 4b 37 75 00 00 00 7f t=23437\n"
    )
    base = _make_w1(tmp_path, legacy=legacy)
    device = GpioDs18b20Sensor(_info("sensor.g", TYPE_SENSOR), base_dir=str(base))
    with pytest.raises(DeviceError, match="CRC"):
        device.read_state()


def test_ds18b20_missing_reports_offline(tmp_path):
    device = GpioDs18b20Sensor(_info("sensor.h", TYPE_SENSOR), base_dir=str(tmp_path / "empty"))
    assert device.is_available() is False
    with pytest.raises(DeviceOffline, match="1-wire"):
        device.read_state()


# ---------------------------------------------------------------- PIR
def test_motion_detection():
    motion = FakeMotion(27)
    device = GpioMotionSensor(_info("sensor.i", TYPE_SENSOR), motion)
    assert device.read_state()["motion_detected"] is False
    motion.value = 1.0
    assert device.read_state()["motion_detected"] is True


# ---------------------------------------------------------------- 清单解析
def _write_config(tmp_path: Path, payload) -> Path:
    path = tmp_path / "gpio_devices.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_file_missing_gives_copy_guidance(tmp_path):
    with pytest.raises(DeviceError, match="复制示例"):
        load_gpio_devices_file(tmp_path / "absent.json")


def test_load_file_rejects_bad_fields(tmp_path):
    with pytest.raises(DeviceError, match="type"):
        _write_and_load(tmp_path, {"devices": [{"id": "x", "name": "X", "type": "robot", "pin": 17}]})
    with pytest.raises(DeviceError, match="kind"):
        _write_and_load(tmp_path, {"devices": [
            {"id": "s", "name": "S", "type": "sensor", "kind": "camera", "pin": 4}]})
    with pytest.raises(DeviceError, match="BCM"):
        _write_and_load(tmp_path, {"devices": [
            {"id": "l", "name": "L", "type": "light", "pin": 28}]})
    with pytest.raises(DeviceError, match="pin"):
        _write_and_load(tmp_path, {"devices": [{"id": "l", "name": "L", "type": "light"}]})


def _write_and_load(tmp_path, payload):
    path = _write_config(tmp_path, payload)
    return load_gpio_devices_file(path)


def test_build_registry_with_fake_factory(tmp_path):
    path = _write_config(tmp_path, VALID_CONFIG)
    config = SimpleNamespace(gpio_devices_file=path)
    factory = FakeFactory()
    registry = build_gpio_registry(config, factory=factory)

    devices = {d.info.id: d for d in registry.list()}
    assert set(devices) == {spec["id"] for spec in VALID_CONFIG["devices"]}
    assert devices["light.strip"].pwm_enabled is True
    assert devices["fan.bench"].supported_actions() == ["turn_on", "turn_off", "set_speed"]
    # 输出引脚按配置传给工厂：继电器 active_high=false、PWM 灯 pwm=true
    pins = {(out.pin, out.pwm, out.active_high) for out in factory.outputs}
    assert (17, False, True) in pins
    assert (18, True, True) in pins
    assert (23, False, False) in pins
    assert [(d.pin, d.model) for d in factory.dhts] == [(4, "dht22")]
    assert [m.pin for m in factory.motions] == [27]
    # 传感器默认离线语义：未接 1-wire 的 DS18B20 可读出离线状态而非崩溃
    assert registry.get("sensor.temp_probe").is_available() is False


def test_build_registry_rejects_empty_and_duplicates(tmp_path):
    config = SimpleNamespace(gpio_devices_file=_write_config(tmp_path, {"devices": []}))
    with pytest.raises(DeviceError, match="为空"):
        build_gpio_registry(config, factory=FakeFactory())

    dup = {"devices": [
        {"id": "light.x", "name": "A", "type": "light", "pin": 17},
        {"id": "light.x", "name": "B", "type": "light", "pin": 18},
    ]}
    config = SimpleNamespace(gpio_devices_file=_write_config(tmp_path, dup))
    with pytest.raises(DeviceError, match="重复注册"):
        build_gpio_registry(config, factory=FakeFactory())


def test_gpiozero_factory_missing_dependency(monkeypatch):
    """没有 gpiozero 的环境（如 Windows CI）：给出可读指引而不是崩溃。"""
    monkeypatch.setitem(sys.modules, "gpiozero", None)  # import gpiozero → ImportError
    with pytest.raises(DeviceError, match="gpiozero 未安装"):
        GpiozeroFactory().make_output(17, pwm=False, active_high=True)


# ---------------------------------------------------------------- 配置层
def _load_with_gpio_backend(tmp_path, monkeypatch, devices_file=None):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DEVICE_BACKEND", "gpio")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    if devices_file:
        monkeypatch.setenv("GPIO_DEVICES_FILE", str(devices_file))
    return load_config(env_path=tmp_path / "missing.env")


def test_config_gpio_requires_devices_file(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="gpio_devices"):
        _load_with_gpio_backend(tmp_path, monkeypatch)


def test_config_gpio_accepts_existing_file(tmp_path, monkeypatch):
    devices_file = _write_config(tmp_path, VALID_CONFIG)
    config = _load_with_gpio_backend(tmp_path, monkeypatch, devices_file=devices_file)
    assert config.device_backend == "gpio"
    assert config.gpio_devices_file == devices_file
    registry = build_gpio_registry(config, factory=FakeFactory())
    assert len(registry.list()) == len(VALID_CONFIG["devices"])


def test_config_rejects_unknown_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DEVICE_BACKEND", "bluetooth")
    with pytest.raises(ConfigError, match="DEVICE_BACKEND"):
        load_config(env_path=tmp_path / "missing.env")
