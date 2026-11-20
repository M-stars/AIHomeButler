"""树莓派 GPIO 设备后端（DEVICE_BACKEND=gpio）：用 40-pin IO 直连并调控硬件。

支持类型（引脚编号统一 BCM，GPIO17 = 物理引脚 11）：
  - light  ：LED/灯带继电器，可选 PWM 调亮度（gpiozero LED/PWMLED）；
  - fan    ：单引脚 PWM 风扇（经三极管/MOS 驱动），三档 low/medium/high 映射占空比；
  - switch ：继电器/功率开关（gpiozero LED），on/off；
  - sensor ：kind=dht11/dht22（adafruit-circuitpython-dht，温度+湿度）、
             kind=ds18b20（内核 1-wire，读 /sys/bus/w1，零额外依赖）、
             kind=motion（PIR 人体感应，gpiozero MotionSensor）。

设计约定（与 simulated/homeassistant 后端语义一致）：
  - 硬件访问全部集中在 PinFactory 抽象后面：Pi 上用 GpiozeroFactory（延迟导入，
    未装 gpiozero 报错含修复指引），测试注入假工厂即可覆盖全部逻辑，不碰真引脚；
  - 上电安全默认：所有输出引脚初始化为关闭，进程重启后设备回到关闭态（如实，
    GPIO 状态不持久化——与继电器"重启复位"的硬件行为一致）；
  - 传感器读失败抛 DeviceOffline（绝不编造数据）；DHT 抖动重试 3 次仍失败才离线；
  - 设备清单来自 config/gpio_devices.json（GPIO_DEVICES_FILE 可换路径），格式非法
    fail-fast 拒绝启动，不给"一半设备"的中间态。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .base import (
    TYPE_FAN,
    TYPE_LIGHT,
    TYPE_SENSOR,
    TYPE_SWITCH,
    ControllableDevice,
    Device,
    DeviceError,
    DeviceInfo,
    DeviceOffline,
)

_log = logging.getLogger("butler.devices.gpio")

VALID_DEVICE_TYPES = (TYPE_SENSOR, TYPE_LIGHT, TYPE_FAN, TYPE_SWITCH)
VALID_SENSOR_KINDS = ("dht11", "dht22", "ds18b20", "motion")
MIN_BCM_PIN = 2      # BCM0/1 为系统保留引脚，禁止使用
MAX_BCM_PIN = 27     # Pi 40-pin header 的 BCM 引脚上限
DEFAULT_SPEED_DUTY = {"low": 0.34, "medium": 0.67, "high": 1.0}
DHT_READ_ATTEMPTS = 3
DHT_RETRY_SECONDS = 2.0
DEFAULT_DS18B20_DIR = "/sys/bus/w1/devices"


# ---------------------------------------------------------------- 引脚工厂
class PinFactory:
    """GPIO 硬件抽象：真实环境用 GpiozeroFactory，测试注入假实现。

    make_output 返回的对象须支持 on()/off() 与 value 属性（0..1 float 可读写）；
    make_motion 返回对象须支持 value 属性（0=无感应，1=有感应）；
    make_dht 返回对象须支持 temperature/humidity 属性（摄氏度/百分比），
    读失败抛 RuntimeError。
    """

    def make_output(self, pin: int, *, pwm: bool, active_high: bool):
        raise NotImplementedError

    def make_motion(self, pin: int):
        raise NotImplementedError

    def make_dht(self, pin: int, model: str):
        raise NotImplementedError


class GpiozeroFactory(PinFactory):
    """gpiozero 实现（Raspberry Pi OS 预装；Bookworm 走 lgpio 后端）。

    全部延迟导入：管家在 Windows/CI 上构建 gpio 后端时给出可读的指引，
    而不是在 import 阶段崩溃。
    """

    @staticmethod
    def _require_gpiozero():
        try:
            import gpiozero  # noqa: F401
        except ImportError as exc:
            raise DeviceError(
                "gpiozero 未安装：GPIO 后端只能在树莓派上使用"
                "（Raspberry Pi OS 已预装；其他系统 pip install gpiozero lgpio）。"
                "非树莓派环境请改用 DEVICE_BACKEND=simulated"
            ) from exc

    def make_output(self, pin: int, *, pwm: bool, active_high: bool):
        self._require_gpiozero()
        from gpiozero import LED, PWMLED

        cls = PWMLED if pwm else LED
        # initial_value 关闭态：上电安全默认（PWMLED=0，LED=False）
        try:
            if pwm:
                return cls(pin, active_high=active_high, initial_value=0)
            return cls(pin, active_high=active_high, initial_value=False)
        except ImportError as exc:  # gpiozero.BadPinFactory：装了包但没有可用引脚后端
            raise DeviceError(
                "GPIO 引脚工厂初始化失败（gpiozero 找不到可用的引脚后端）。"
                "树莓派上执行：sudo apt install -y python3-gpiozero lgpio；"
                "非树莓派环境请改用 DEVICE_BACKEND=simulated"
            ) from exc

    def make_motion(self, pin: int):
        self._require_gpiozero()
        from gpiozero import MotionSensor

        try:
            return MotionSensor(pin, queue_len=8, sample_rate=5)
        except ImportError as exc:
            raise DeviceError(
                "GPIO 引脚工厂初始化失败（gpiozero 找不到可用的引脚后端）。"
                "树莓派上执行：sudo apt install -y python3-gpiozero lgpio"
            ) from exc

    def make_dht(self, pin: int, model: str):
        try:
            import adafruit_dht
            import board
        except ImportError as exc:
            raise DeviceOffline(
                f"读取 {model} 需要依赖：pip install adafruit-circuitpython-dht"
                "（另需 sudo apt install libgpiod2）"
            ) from exc
        board_pin = getattr(board, f"D{pin}", None)
        if board_pin is None:
            raise DeviceError(f"引脚 BCM{pin} 不是可用的 DHT 引脚（board.D{pin} 不存在）")
        # Linux SBC 标准做法 use_pulseio=False（走位检测实现，无需 blinka pulseio）
        return adafruit_dht.DHT22(board_pin, use_pulseio=False) if model == "dht22" \
            else adafruit_dht.DHT11(board_pin, use_pulseio=False)


# ---------------------------------------------------------------- 输出设备
class GpioOutputDevice(ControllableDevice):
    """开关型输出的公共实现：维护内存状态 + 回读真实引脚。"""

    def __init__(self, info: DeviceInfo, output, *, pwm: bool = False) -> None:
        super().__init__(info)
        self._output = output
        self._pwm = bool(pwm)

    @property
    def pwm_enabled(self) -> bool:
        return self._pwm

    def is_available(self) -> bool:
        return True  # 本地引脚始终可达（无网络层）

    def _set_raw(self, value: float) -> None:
        """写引脚：0=关。PWM 输出写占空比；非 PWM 输出只接受开/关
        （gpiozero LED.value 只支持 0/1）。异常收敛为 DeviceOffline。"""
        try:
            if value <= 0:
                self._output.off()
            elif self._pwm:
                self._output.value = min(1.0, float(value))
            else:
                self._output.on()
        except Exception as exc:
            raise DeviceOffline(f"{self.info.name}（{self.info.id}）写引脚失败：{exc}") from exc

    def _raw_value(self) -> float:
        try:
            return float(self._output.value)
        except Exception as exc:
            raise DeviceOffline(f"{self.info.name}（{self.info.id}）读引脚失败：{exc}") from exc


class GpioLight(GpioOutputDevice):
    """灯：开关 + 亮度（1-100，需 pwm=true 启用 PWM）。"""

    ACTIONS: List[str] = ["turn_on", "turn_off", "set_brightness"]

    def __init__(self, info: DeviceInfo, output, *, pwm: bool = False,
                 initial_brightness: int = 80) -> None:
        super().__init__(info, output, pwm=pwm)
        self._brightness = max(1, min(100, int(initial_brightness)))

    def read_state(self) -> Dict:
        raw = self._raw_value()
        # 与模拟后端语义一致：关闭时仍报告记忆的亮度（供 set_brightness 的
        # 执行后验证比对；真实点亮亮度以开灯时的记忆值为准）
        return {"on": raw > 0, "brightness": self._brightness}

    def supported_actions(self) -> List[str]:
        if self._pwm:
            return list(self.ACTIONS)
        return ["turn_on", "turn_off"]

    def apply(self, action: str, value=None) -> Dict:
        if action == "turn_on":
            if value is not None:
                if not self._pwm:
                    raise DeviceError(
                        f"{self.info.name} 未启用 PWM（配置 pwm=true），不支持亮度"
                    )
                self._brightness = self._parse_brightness(value)
            self._set_raw(self._brightness / 100.0)
        elif action == "turn_off":
            self._set_raw(0.0)
        elif action == "set_brightness":
            if not self._pwm:
                raise DeviceError(
                    f"{self.info.name} 未启用 PWM（配置 pwm=true），不支持亮度调节"
                )
            self._brightness = self._parse_brightness(value)
            if self._raw_value() > 0:
                self._set_raw(self._brightness / 100.0)
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.supported_actions())}"
            )
        return self.read_state()

    @staticmethod
    def _parse_brightness(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise DeviceError("亮度必须是 1-100 的整数")
        try:
            brightness = int(value)
        except ValueError:
            raise DeviceError("亮度必须是 1-100 的整数") from None
        if not 1 <= brightness <= 100:
            raise DeviceError("亮度必须在 1-100 之间")
        return brightness


class GpioFan(GpioOutputDevice):
    """风扇：开关 + 三档风速（low/medium/high → PWM 占空比）。"""

    ACTIONS: List[str] = ["turn_on", "turn_off", "set_speed"]
    SPEEDS: List[str] = ["low", "medium", "high"]

    def __init__(self, info: DeviceInfo, output,
                 speed_duty: Optional[Dict[str, float]] = None) -> None:
        super().__init__(info, output, pwm=True)
        duty = dict(DEFAULT_SPEED_DUTY)
        for key, val in (speed_duty or {}).items():
            if key not in self.SPEEDS:
                raise DeviceError(f"风扇档位只能是 {'/'.join(self.SPEEDS)}，非法档位：{key!r}")
            num = float(val)
            if not 0.0 < num <= 1.0:
                raise DeviceError(f"档位 {key} 的占空比必须是 (0, 1]，当前：{val!r}")
            duty[key] = num
        self._duty = duty
        self._speed = "medium"

    def read_state(self) -> Dict:
        raw = self._raw_value()
        return {"on": raw > 0, "speed": self._speed}

    def supported_actions(self) -> List[str]:
        return list(self.ACTIONS)

    def apply(self, action: str, value=None) -> Dict:
        if action == "turn_on":
            if value is not None:
                self._speed = self._parse_speed(value)
            self._set_raw(self._duty[self._speed])
        elif action == "turn_off":
            self._set_raw(0.0)
        elif action == "set_speed":
            # 与模拟后端语义一致：set_speed 只改档位，不改变开关状态；
            # 运行中则即时切换占空比
            self._speed = self._parse_speed(value)
            if self._raw_value() > 0:
                self._set_raw(self._duty[self._speed])
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.ACTIONS)}"
            )
        return self.read_state()

    def _parse_speed(self, value) -> str:
        if not isinstance(value, str) or value not in self.SPEEDS:
            raise DeviceError(f"风速档位必须是 {'/'.join(self.SPEEDS)}")
        return value


class GpioSwitch(GpioOutputDevice):
    """继电器/功率开关：on/off。"""

    ACTIONS: List[str] = ["turn_on", "turn_off"]

    def read_state(self) -> Dict:
        return {"on": self._raw_value() > 0}

    def supported_actions(self) -> List[str]:
        return list(self.ACTIONS)

    def apply(self, action: str, value=None) -> Dict:
        if action == "turn_on":
            self._set_raw(1.0)
        elif action == "turn_off":
            self._set_raw(0.0)
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.ACTIONS)}"
            )
        return self.read_state()


# ---------------------------------------------------------------- 传感器
class GpioDhtSensor(Device):
    """DHT11/DHT22 温湿度传感器（adafruit-circuitpython-dht）。

    DHT 单总线协议抖动常见：单次读失败自动重试，重试耗尽才报离线，
    不编造数据（与 HARDWARE.md 的"不做假数据回填"约定一致）。
    """

    def __init__(self, info: DeviceInfo, reader, *, retries: int = DHT_READ_ATTEMPTS,
                 retry_seconds: float = DHT_RETRY_SECONDS) -> None:
        super().__init__(info)
        self._reader = reader
        self._retries = max(1, int(retries))
        self._retry_seconds = float(retry_seconds)
        self._init_error: Optional[str] = None
        try:
            self._dht = reader()  # 延迟导入发生在工厂里；缺依赖在此暴露
        except DeviceError as exc:
            self._init_error = str(exc)
        except DeviceOffline as exc:
            self._init_error = str(exc)
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}"

    def is_available(self) -> bool:
        return self._init_error is None

    def read_state(self) -> Dict:
        if self._init_error is not None:
            raise DeviceOffline(f"{self.info.name}（{self.info.id}）不可用：{self._init_error}")
        last_error: Optional[Exception] = None
        for attempt in range(self._retries):
            try:
                temperature = float(self._dht.temperature)
                humidity = float(self._dht.humidity)
                return {
                    "temperature_c": round(temperature, 1),
                    "humidity_percent": round(humidity, 1),
                    "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "fresh": True,
                }
            except RuntimeError as exc:  # DHT 校验失败/超时的典型异常
                last_error = exc
                if attempt < self._retries - 1:
                    time.sleep(self._retry_seconds)
            except DeviceError:
                raise
            except Exception as exc:
                raise DeviceOffline(
                    f"{self.info.name}（{self.info.id}）读取失败：{exc}"
                ) from exc
        raise DeviceOffline(
            f"{self.info.name}（{self.info.id}）连续 {self._retries} 次读取失败"
            f"（检查 DATA 线接线与供电）：{last_error}"
        )


class GpioDs18b20Sensor(Device):
    """DS18B20 温度传感器：内核 1-wire 接口（需 dtoverlay=w1-gpio），零额外依赖。"""

    def __init__(self, info: DeviceInfo, base_dir: str = DEFAULT_DS18B20_DIR) -> None:
        super().__init__(info)
        self._base_dir = Path(base_dir)

    def _slave_dir(self) -> Optional[Path]:
        if not self._base_dir.exists():
            return None
        for path in sorted(self._base_dir.glob("28-*")):
            if (path / "temperature").exists() or (path / "w1_slave").exists():
                return path
        return None

    def is_available(self) -> bool:
        return self._slave_dir() is not None

    def read_state(self) -> Dict:
        slave = self._slave_dir()
        if slave is None:
            raise DeviceOffline(
                f"{self.info.name}（{self.info.id}）未发现 DS18B20（检查 1-wire："
                "raspi-config 开启 1-Wire 或 /boot/config.txt 加 dtoverlay=w1-gpio）"
            )
        try:
            milli_c = self._read_temperature(slave)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceOffline(
                f"{self.info.name}（{self.info.id}）读取失败：{exc}"
            ) from exc

        return {
            "temperature_c": round(milli_c / 1000.0, 1),
            "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "fresh": True,
        }

    @staticmethod
    def _read_temperature(slave_dir: Path) -> float:
        temp_file = slave_dir / "temperature"  # 新内核：直接毫摄氏度
        if temp_file.exists():
            return float(temp_file.read_text(encoding="ascii").strip())
        legacy = slave_dir / "w1_slave"  # 旧内核：两行 CRC + t=xxxx
        for line in legacy.read_text(encoding="ascii").splitlines():
            if "crc=" in line and "YES" not in line:
                raise DeviceError(f"DS18B20 CRC 校验失败（{slave_dir.name}）")
            if "t=" in line:
                return float(line.split("t=")[1])  # t=23500 即 23500 m°C = 23.5°C
        raise DeviceError(f"DS18B20 数据文件无法解析（{slave_dir.name}）")


class GpioMotionSensor(Device):
    """PIR 人体感应（gpiozero MotionSensor）。"""

    def __init__(self, info: DeviceInfo, sensor) -> None:
        super().__init__(info)
        self._sensor = sensor

    def is_available(self) -> bool:
        return True

    def read_state(self) -> Dict:

        try:
            motion = float(self._sensor.value) > 0.5
        except Exception as exc:
            raise DeviceOffline(f"{self.info.name}（{self.info.id}）读取失败：{exc}") from exc
        return {
            "motion_detected": motion,
            "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------- 配置解析与装配
def _parse_spec(raw: dict, index: int) -> dict:
    """校验单条设备配置（fail-fast：任何字段非法直接 DeviceError）。"""
    where = f"gpio_devices.json 第 {index + 1} 条"
    if not isinstance(raw, dict):
        raise DeviceError(f"{where}：必须是对象")
    device_id = str(raw.get("id", "")).strip()
    if not device_id:
        raise DeviceError(f"{where}：缺少 id")
    name = str(raw.get("name", "")).strip() or device_id
    device_type = str(raw.get("type", "")).strip().lower()
    if device_type not in VALID_DEVICE_TYPES:
        raise DeviceError(
            f"{where}（{device_id}）：type 只能是 {'/'.join(VALID_DEVICE_TYPES)}，当前：{device_type!r}"
        )
    area = str(raw.get("area", "")).strip()

    spec = {"id": device_id, "name": name, "type": device_type, "area": area}

    if device_type == TYPE_SENSOR:
        kind = str(raw.get("kind", "")).strip().lower()
        if kind not in VALID_SENSOR_KINDS:
            raise DeviceError(
                f"{where}（{device_id}）：sensor 的 kind 只能是 "
                f"{'/'.join(VALID_SENSOR_KINDS)}，当前：{kind!r}"
            )
        spec["kind"] = kind
        if kind != "ds18b20":
            spec["pin"] = _parse_pin(raw, where, device_id)
        spec["w1_dir"] = str(raw.get("w1_dir", "")).strip() or DEFAULT_DS18B20_DIR
        return spec

    spec["pin"] = _parse_pin(raw, where, device_id)
    spec["active_high"] = bool(raw.get("active_high", True))
    if device_type == TYPE_LIGHT:
        spec["pwm"] = bool(raw.get("pwm", False))
        spec["initial_brightness"] = max(1, min(100, _to_int(
            raw.get("initial_brightness", 80), f"{where}（{device_id}）initial_brightness", 80)))
    if device_type == TYPE_FAN:
        speeds = raw.get("speeds")
        if speeds is not None:
            if not isinstance(speeds, dict):
                raise DeviceError(f"{where}（{device_id}）：speeds 必须是对象，如 {{\"low\": 0.3}}")
            spec["speeds"] = speeds
    return spec


def _parse_pin(raw: dict, where: str, device_id: str) -> int:
    pin = raw.get("pin")
    if isinstance(pin, bool) or not isinstance(pin, (int, str)):
        raise DeviceError(f"{where}（{device_id}）：缺少或非法的 pin（BCM 编号 2-27）")
    try:
        pin = int(pin)
    except ValueError:
        raise DeviceError(
            f"{where}（{device_id}）：pin 必须是 BCM 编号整数（2-27），当前：{raw.get('pin')!r}"
        ) from None
    if not MIN_BCM_PIN <= pin <= MAX_BCM_PIN:
        raise DeviceError(
            f"{where}（{device_id}）：pin 超出 BCM 引脚范围（{MIN_BCM_PIN}-{MAX_BCM_PIN}），当前：{pin}"
        )
    return pin


def _to_int(value, where: str, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DeviceError(f"{where} 必须是整数，当前：{value!r}") from None


def load_gpio_devices_file(path: Path) -> List[dict]:
    """读取设备清单文件（JSON）。文件缺失/非法给出可读指引。"""
    path = Path(path)
    if not path.exists():
        raise DeviceError(
            f"GPIO 设备清单不存在：{path}。请复制示例并按实际接线修改："
            f"cp {path.parent / 'gpio_devices.example.json'} {path}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeviceError(f"GPIO 设备清单解析失败（{path}）：{exc}") from exc
    devices = raw.get("devices") if isinstance(raw, dict) else raw
    if not isinstance(devices, list):
        raise DeviceError(f"GPIO 设备清单格式非法（{path}）：需要 {{\"devices\": [...]}}")
    return [_parse_spec(item, index) for index, item in enumerate(devices)]


def _build_device(spec: dict, factory: PinFactory) -> Device:
    info = DeviceInfo(
        id=spec["id"], name=spec["name"], device_type=spec["type"], area=spec["area"]
    )
    device_type = spec["type"]
    if device_type == TYPE_LIGHT:
        output = factory.make_output(spec["pin"], pwm=spec["pwm"],
                                     active_high=spec["active_high"])
        return GpioLight(info, output, pwm=spec["pwm"],
                         initial_brightness=spec["initial_brightness"])
    if device_type == TYPE_FAN:
        output = factory.make_output(spec["pin"], pwm=True,
                                     active_high=spec["active_high"])
        return GpioFan(info, output, speed_duty=spec.get("speeds"))
    if device_type == TYPE_SWITCH:
        output = factory.make_output(spec["pin"], pwm=False,
                                     active_high=spec["active_high"])
        return GpioSwitch(info, output)
    if device_type == TYPE_SENSOR:
        kind = spec["kind"]
        if kind in ("dht11", "dht22"):
            return GpioDhtSensor(info, lambda: factory.make_dht(spec["pin"], kind))
        if kind == "ds18b20":
            return GpioDs18b20Sensor(info, base_dir=spec["w1_dir"])
        if kind == "motion":
            return GpioMotionSensor(info, factory.make_motion(spec["pin"]))
    raise DeviceError(f"未支持的 GPIO 设备类型：{device_type!r}")  # pragma: no cover


def build_gpio_registry(config, *, factory: Optional[PinFactory] = None) -> "DeviceRegistry":
    """按 GPIO_DEVICES_FILE 构建设备注册表。空清单/重复 ID 拒绝启动。

    factory 参数供测试注入假引脚（不碰真实 GPIO）；生产环境用 GpiozeroFactory。
    """
    from .registry import DeviceRegistry

    spec_list = load_gpio_devices_file(config.gpio_devices_file)
    if not spec_list:
        raise DeviceError(
            f"GPIO 设备清单为空（{config.gpio_devices_file}）：至少配置一个设备，"
            "否则请改用 DEVICE_BACKEND=simulated"
        )
    factory = factory or GpiozeroFactory()
    registry = DeviceRegistry()
    for spec in spec_list:
        registry.register(_build_device(spec, factory))
    _log.info("GPIO 后端就绪：%d 个设备（清单 %s）", len(spec_list), config.gpio_devices_file)
    return registry
