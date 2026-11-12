"""Home Assistant 适配器：通过官方 REST API 接入真实设备。

只有本模块知道 HA 的存在；Agent 与工具层继续面向 Device 抽象接口。
API 语义：https://developers.home-assistant.io/docs/api/rest/

错误语义映射（与 Device 抽象约定一致）：
  - 传输层失败（读）              → DeviceOffline（设备/平台离线）
  - 传输层失败（控制，结果不明）   → StateUnknownError（不得报成功也不得报失败）
  - HTTP 401                       → HassApiError（令牌配置错误）
  - HTTP 404                       → DeviceNotFound
  - 其他 4xx/5xx                   → HassApiError

安全设计：只有 config/ha_devices.json 白名单中的实体才会暴露给 AI。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .base import (
    TYPE_CLIMATE,
    TYPE_COVER,
    TYPE_FAN,
    TYPE_HUMIDIFIER,
    TYPE_LIGHT,
    TYPE_SENSOR,
    TYPE_SWITCH,
    TYPE_VACUUM,
    ControllableDevice,
    Device,
    DeviceError,
    DeviceInfo,
    DeviceNotFound,
    DeviceOffline,
    StateUnknownError,
)

_HA_OFFLINE_STATES = ("unavailable", "unknown")
_FRESH_WINDOW_SECONDS = 120.0
# 风扇百分比与档位的映射边界（与 HA fan 的 percentage 语义对齐）
_SPEED_TO_PERCENT = {"low": 33, "medium": 66, "high": 100}


class HassApiError(DeviceError):
    """HA API 返回错误（认证失败、服务端错误等）。"""


class HassClient:
    """极简 HA REST API 客户端：读状态 + 调服务。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 10.0,
        logger: logging.Logger = None,
    ) -> None:
        if not base_url:
            raise HassApiError("HA_URL 未配置")
        if not token:
            raise HassApiError("HA_TOKEN 未配置（HA 个人资料页底部生成长期访问令牌）")
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout_seconds
        self._log = logger or logging.getLogger("butler.ha")

    def get_state(self, entity_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/states/{entity_id}", ambiguous=False)

    def get_states(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/states", ambiguous=False)

    def call_service(
        self, domain: str, service: str, entity_id: str, data: Optional[dict] = None
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"entity_id": entity_id}
        if data:
            payload.update(data)
        return self._request(
            "POST", f"/api/services/{domain}/{service}", json=payload, ambiguous=True
        )

    def _request(
        self, method: str, path: str, json: Optional[dict] = None, ambiguous: bool = False
    ):
        url = self._base + path
        try:
            # trust_env=False：HA 在局域网，绝不能被 HTTP_PROXY 等环境代理接管——
            # 代理返回的 5xx 会被误判为 API 错误，而非离线/结果不明
            response = httpx.request(
                method, url, headers=self._headers, json=json,
                timeout=self._timeout, trust_env=False,
            )
        except httpx.TransportError as exc:
            # 覆盖全部传输层错误（拒连/超时/对端中断等）；
            # 控制类请求失败时结果不明：指令可能已到达 HA
            if ambiguous:
                raise StateUnknownError(
                    f"控制指令可能已提交到 Home Assistant，但无法确认结果（{type(exc).__name__}）"
                ) from exc
            raise DeviceOffline(
                f"无法连接 Home Assistant（{self._base}）：{type(exc).__name__}"
            ) from exc

        if response.status_code == 401:
            raise HassApiError("Home Assistant 认证失败（401）：请检查 .env 中的 HA_TOKEN")
        if response.status_code == 404:
            raise DeviceNotFound(f"Home Assistant 中不存在该实体或接口：{path}")
        if response.status_code >= 400:
            raise HassApiError(
                f"Home Assistant API 错误（HTTP {response.status_code}）：{response.text[:200]}"
            )
        return response.json()


# ---------------------------------------------------------------- 能力位标志（镜像 HA EntityFeature 常量）
# 镜像 homeassistant/components/<domain>/const.py，数值多年稳定；只做按位与，
# HA 新增位对本模块安全无感。新版 HA 的灯亮度能力走 supported_color_modes。
FAN_SET_SPEED = 1
COVER_OPEN, COVER_CLOSE, COVER_SET_POSITION, COVER_STOP = 1, 2, 4, 8
CLIMATE_TARGET_TEMPERATURE, CLIMATE_TARGET_TEMPERATURE_RANGE = 1, 2
VACUUM_TURN_ON, VACUUM_PAUSE, VACUUM_STOP, VACUUM_RETURN_HOME = 1, 4, 8, 16
VACUUM_START = 8192
LIGHT_LEGACY_BRIGHTNESS = 1


def _light_supports_brightness(features: Dict[str, Any]) -> bool:
    modes = features.get("supported_color_modes")
    if isinstance(modes, list) and modes:
        return any(mode != "onoff" for mode in modes)
    if "brightness" in features:
        return True
    return bool(int(features.get("supported_features", 0) or 0) & LIGHT_LEGACY_BRIGHTNESS)


def supported_actions_for(domain: str, features: Dict[str, Any]) -> Optional[List[str]]:
    """从 HA 能力快照推导某域实体支持的动作；None 表示推导不出（实体应跳过）。

    discovery 模块用它筛选设备，适配器构造时用它生成 actions——单一事实来源。
    """
    try:
        bits = 0 if isinstance(features.get("supported_features"), bool) else int(
            features.get("supported_features", 0) or 0
        )
    except (TypeError, ValueError):
        bits = 0
    if domain == TYPE_LIGHT:
        actions = ["turn_on", "turn_off"]
        if _light_supports_brightness(features):
            actions.append("set_brightness")
        return actions
    if domain == TYPE_FAN:
        actions = ["turn_on", "turn_off"]
        if "percentage" in features or "percentage_step" in features or (bits & FAN_SET_SPEED):
            actions.append("set_speed")
        return actions
    if domain == TYPE_SWITCH:
        return ["turn_on", "turn_off"]
    if domain == TYPE_COVER:
        # 键存在才走位标志推导（显式 0 = 实体无任何 cover 能力 → None 跳过）；
        # 键缺失才是旧实体（无位标志时代），按全集
        if "supported_features" in features:
            actions = []
            if bits & COVER_OPEN:
                actions.append("open")
            if bits & COVER_CLOSE:
                actions.append("close")
            if bits & COVER_STOP:
                actions.append("stop")
            if bits & COVER_SET_POSITION:
                actions.append("set_position")
            return actions or None
        return ["open", "close", "stop", "set_position"]
    if domain == TYPE_CLIMATE:
        # 双设定点（heat_cool，无单 temperature 属性）无法唯一回读验证，结构性排除
        if "temperature" not in features and (
            "target_temp_high" in features or "target_temp_low" in features
        ):
            return None
        actions = ["turn_on", "turn_off"]
        if "temperature" in features or (bits & CLIMATE_TARGET_TEMPERATURE):
            actions.append("set_temperature")
        return actions
    if domain == TYPE_HUMIDIFIER:
        actions = ["turn_on", "turn_off"]
        if "humidity" in features or "min_humidity" in features or "max_humidity" in features:
            actions.append("set_humidity")
        return actions
    if domain == TYPE_VACUUM:
        actions = []
        if bits & (VACUUM_START | VACUUM_TURN_ON):
            actions.append("start")
        if bits & VACUUM_PAUSE:
            actions.append("pause")
        if bits & VACUUM_STOP:
            actions.append("stop")
        if bits & VACUUM_RETURN_HOME:
            actions.append("return_to_base")
        return actions or None
    return None


class _HaDeviceBase(Device):
    """HA 实体的公共封装：可用性判断 + 状态读取骨架。"""

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(info)
        self._client = client
        # 能力快照（自动发现传入）；None = 白名单旧路径，按类默认动作全集
        self._features = features

    def _resolved_actions(self, default: List[str]) -> List[str]:
        if self._features is None:
            return list(default)
        return supported_actions_for(self.info.device_type, self._features) or list(default)

    def _raw_state(self) -> Dict[str, Any]:
        return self._client.get_state(self.info.id)

    @staticmethod
    def _is_offline(raw: Dict[str, Any]) -> bool:
        return raw.get("state") in _HA_OFFLINE_STATES

    def is_available(self) -> bool:
        try:
            return not self._is_offline(self._raw_state())
        except DeviceOffline:
            return False

    def read_state(self) -> Dict[str, Any]:
        raw = self._raw_state()
        if self._is_offline(raw):
            raise DeviceOffline(
                f"{self.info.name}({self.info.id}) 在 Home Assistant 中不可用"
                f"（state={raw.get('state')!r}）"
            )
        return self._map_state(raw)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class HaSensor(_HaDeviceBase):
    """通用 HA 传感器：数值 + 单位 + 采样时间与新鲜度。"""

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        attributes = raw.get("attributes") or {}
        raw_value = raw.get("state")
        try:
            value: Optional[float] = float(raw_value)
        except (TypeError, ValueError):
            value = None
        sampled_at = raw.get("last_updated") or raw.get("last_changed")
        state: Dict[str, Any] = {
            "value": value,
            "raw_state": raw_value,
            "unit": attributes.get("unit_of_measurement", ""),
            "sampled_at": sampled_at,
        }
        if sampled_at:
            state["fresh"] = self._is_fresh(sampled_at)
        return state

    @staticmethod
    def _is_fresh(sampled_at: str) -> bool:
        try:
            sampled = datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
            if sampled.tzinfo is None:
                sampled = sampled.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - sampled).total_seconds()
            return age <= _FRESH_WINDOW_SECONDS
        except (ValueError, TypeError, OverflowError):
            return False


class HaLight(_HaDeviceBase, ControllableDevice):
    """HA 灯实体：开关 + 亮度（HA 0-255 ↔ 本系统 1-100）。

    发现模式按 supported_color_modes 推导：纯开关灯（onoff）不含 set_brightness。
    """

    DEFAULT_ACTIONS = ["turn_on", "turn_off", "set_brightness"]

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(client, info, features)
        self.actions = self._resolved_actions(self.DEFAULT_ACTIONS)

    def supported_actions(self) -> List[str]:
        return list(self.actions)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        attributes = raw.get("attributes") or {}
        brightness_raw = attributes.get("brightness")
        state: Dict[str, Any] = {"on": raw.get("state") == "on"}
        if brightness_raw is not None:
            try:
                state["brightness"] = max(1, round(int(brightness_raw) * 100 / 255))
            except (TypeError, ValueError):
                state["brightness"] = None
        else:
            state["brightness"] = None
        return state

    def apply(self, action: str, value=None) -> Dict[str, Any]:
        if action == "turn_on":
            data: Dict[str, Any] = {}
            if value is not None:
                data["brightness"] = self._to_ha_brightness(value)
            self._client.call_service("light", "turn_on", self.info.id, data or None)
        elif action == "turn_off":
            self._client.call_service("light", "turn_off", self.info.id)
        elif action == "set_brightness":
            if value is None:
                raise DeviceError("set_brightness 需要提供 1-100 的亮度值")
            self._client.call_service(
                "light", "turn_on", self.info.id, {"brightness": self._to_ha_brightness(value)}
            )
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.actions)}"
            )
        return self.read_state()

    @staticmethod
    def _to_ha_brightness(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise DeviceError("亮度必须是 1-100 的整数")
        try:
            brightness = int(value)
        except ValueError:
            raise DeviceError("亮度必须是 1-100 的整数") from None
        if not 1 <= brightness <= 100:
            raise DeviceError("亮度必须在 1-100 之间")
        return max(1, round(brightness * 255 / 100))


class HaFan(_HaDeviceBase, ControllableDevice):
    """HA 风扇实体：开关 + 档位（low/medium/high ↔ percentage 33/66/100）。"""

    DEFAULT_ACTIONS = ["turn_on", "turn_off", "set_speed"]

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(client, info, features)
        self.actions = self._resolved_actions(self.DEFAULT_ACTIONS)

    def supported_actions(self) -> List[str]:
        return list(self.actions)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        attributes = raw.get("attributes") or {}
        percentage = attributes.get("percentage", 66)
        try:
            percentage = int(percentage)
        except (TypeError, ValueError):
            percentage = 66
        return {
            "on": raw.get("state") == "on",
            "speed": _percent_to_speed(percentage),
        }

    def apply(self, action: str, value=None) -> Dict[str, Any]:
        if action == "turn_on":
            data: Dict[str, Any] = {}
            if value is not None:
                data["percentage"] = self._to_percentage(value)
            self._client.call_service("fan", "turn_on", self.info.id, data or None)
        elif action == "turn_off":
            self._client.call_service("fan", "turn_off", self.info.id)
        elif action == "set_speed":
            if value is None:
                raise DeviceError("set_speed 需要提供 low/medium/high")
            self._client.call_service(
                "fan", "turn_on", self.info.id, {"percentage": self._to_percentage(value)}
            )
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.actions)}"
            )
        return self.read_state()

    @staticmethod
    def _to_percentage(value) -> int:
        if not isinstance(value, str) or value not in _SPEED_TO_PERCENT:
            raise DeviceError(f"风速档位必须是 {'/'.join(_SPEED_TO_PERCENT)}")
        return _SPEED_TO_PERCENT[value]


def _percent_to_speed(percentage: int) -> str:
    if percentage <= 33:
        return "low"
    if percentage <= 66:
        return "medium"
    return "high"


class HaSwitch(_HaDeviceBase, ControllableDevice):
    """HA 开关/插座实体：纯开/关。"""

    DEFAULT_ACTIONS = ["turn_on", "turn_off"]

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(client, info, features)
        self.actions = self._resolved_actions(self.DEFAULT_ACTIONS)

    def supported_actions(self) -> List[str]:
        return list(self.actions)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return {"on": raw.get("state") == "on"}

    def apply(self, action: str, value=None) -> Dict[str, Any]:
        if action == "turn_on":
            self._client.call_service("switch", "turn_on", self.info.id)
        elif action == "turn_off":
            self._client.call_service("switch", "turn_off", self.info.id)
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.actions)}"
            )
        return self.read_state()


class HaCover(_HaDeviceBase, ControllableDevice):
    """HA 窗帘/卷帘实体：开/关/停/位置（0-100，100=全开）。

    窗帘动作是异步的（真实窗帘开合需要时间），执行后状态常为
    opening/closing（进行中）。控制验证语义是"指令已接受并开始移动"，
    终态用 get_device_state 查询。
    """

    ACTIONS = ["open", "close", "stop", "set_position"]
    DEFAULT_ACTIONS = ["open", "close", "stop", "set_position"]

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(client, info, features)
        self.actions = self._resolved_actions(self.DEFAULT_ACTIONS)

    def supported_actions(self) -> List[str]:
        return list(self.actions)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        attributes = raw.get("attributes") or {}
        position_raw = attributes.get("current_position")
        try:
            position: Optional[int] = max(0, min(100, int(position_raw)))
        except (TypeError, ValueError):
            position = None
        return {
            "open": raw.get("state") == "open",
            "position": position,
            "activity": raw.get("state"),
        }

    def apply(self, action: str, value=None) -> Dict[str, Any]:
        if action == "open":
            self._client.call_service("cover", "open_cover", self.info.id)
        elif action == "close":
            self._client.call_service("cover", "close_cover", self.info.id)
        elif action == "stop":
            self._client.call_service("cover", "stop_cover", self.info.id)
        elif action == "set_position":
            self._client.call_service(
                "cover", "set_cover_position",
                self.info.id, {"position": self._to_position(value)},
            )
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.actions)}"
            )
        return self.read_state()

    @staticmethod
    def _to_position(value) -> int:
        try:
            if isinstance(value, bool):
                raise TypeError
            number = int(value)
        except (TypeError, ValueError):
            raise DeviceError("set_position 需要提供 0-100 的整数（100=全开）") from None
        if not 0 <= number <= 100:
            raise DeviceError("set_position 需要提供 0-100 的整数（100=全开）")
        return number


class HaClimate(_HaDeviceBase, ControllableDevice):
    """HA 空调/温控实体：开关 + 目标温度。

    仅支持单设定点实体（attributes 带 temperature）；heat_cool 双设定点
    （如 ecobee）回读无法唯一验证目标值，白名单应排除此类实体。
    """

    ACTIONS = ["turn_on", "turn_off", "set_temperature"]
    DEFAULT_ACTIONS = ["turn_on", "turn_off", "set_temperature"]

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(client, info, features)
        self.actions = self._resolved_actions(self.DEFAULT_ACTIONS)

    def supported_actions(self) -> List[str]:
        return list(self.actions)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        attributes = raw.get("attributes") or {}
        return {
            "on": raw.get("state") != "off",
            "hvac_mode": raw.get("state"),
            "current_temperature": attributes.get("current_temperature"),
            "temperature": attributes.get("temperature"),
        }

    def apply(self, action: str, value=None) -> Dict[str, Any]:
        if action == "turn_on":
            self._client.call_service("climate", "turn_on", self.info.id)
        elif action == "turn_off":
            self._client.call_service("climate", "turn_off", self.info.id)
        elif action == "set_temperature":
            self._client.call_service(
                "climate", "set_temperature",
                self.info.id, {"temperature": self._to_temperature(value)},
            )
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.actions)}"
            )
        return self.read_state()

    @staticmethod
    def _to_temperature(value) -> float:
        try:
            if isinstance(value, bool):
                raise TypeError
            number = float(value)
        except (TypeError, ValueError):
            raise DeviceError("set_temperature 需要提供数字温度值（如 26 或 26.5）") from None
        # 宽区间同时覆盖 °C(16-30) 与 °F(45-95) 实体；设备级越界由 HA 拒绝
        if not 5 <= number <= 100:
            raise DeviceError("set_temperature 超出合理范围（5-100）")
        return number


class HaHumidifier(_HaDeviceBase, ControllableDevice):
    """HA 加湿/除湿器实体：开关 + 目标湿度（0-100）。"""

    ACTIONS = ["turn_on", "turn_off", "set_humidity"]
    DEFAULT_ACTIONS = ["turn_on", "turn_off", "set_humidity"]

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(client, info, features)
        self.actions = self._resolved_actions(self.DEFAULT_ACTIONS)

    def supported_actions(self) -> List[str]:
        return list(self.actions)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        attributes = raw.get("attributes") or {}
        return {
            "on": raw.get("state") == "on",
            "humidity": attributes.get("humidity"),
            "current_humidity": attributes.get("current_humidity"),
        }

    def apply(self, action: str, value=None) -> Dict[str, Any]:
        if action == "turn_on":
            self._client.call_service("humidifier", "turn_on", self.info.id)
        elif action == "turn_off":
            self._client.call_service("humidifier", "turn_off", self.info.id)
        elif action == "set_humidity":
            self._client.call_service(
                "humidifier", "set_humidity",
                self.info.id, {"humidity": self._to_humidity(value)},
            )
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.actions)}"
            )
        return self.read_state()

    @staticmethod
    def _to_humidity(value) -> int:
        try:
            if isinstance(value, bool):
                raise TypeError
            number = int(value)
        except (TypeError, ValueError):
            raise DeviceError("set_humidity 需要提供 0-100 的整数") from None
        if not 0 <= number <= 100:
            raise DeviceError("set_humidity 需要提供 0-100 的整数")
        return number


class HaVacuum(_HaDeviceBase, ControllableDevice):
    """HA 扫地机器人实体：启动/暂停/停止/回充。"""

    ACTIONS = ["start", "pause", "stop", "return_to_base"]
    DEFAULT_ACTIONS = ["start", "pause", "stop", "return_to_base"]
    _SERVICES = {
        "start": "start",
        "pause": "pause",
        "stop": "stop",
        "return_to_base": "return_to_base",
    }

    def __init__(self, client: HassClient, info: DeviceInfo,
                 features: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(client, info, features)
        self.actions = self._resolved_actions(self.DEFAULT_ACTIONS)

    def supported_actions(self) -> List[str]:
        return list(self.actions)

    def _map_state(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        attributes = raw.get("attributes") or {}
        battery_raw = attributes.get("battery_level")
        try:
            battery: Optional[int] = int(battery_raw)
        except (TypeError, ValueError):
            battery = None
        return {"status": raw.get("state"), "battery": battery}

    def apply(self, action: str, value=None) -> Dict[str, Any]:
        service = self._SERVICES.get(action)
        if service is None:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.actions)}"
            )
        self._client.call_service("vacuum", service, self.info.id)
        return self.read_state()


_DEVICE_TYPES = {
    TYPE_SENSOR: HaSensor,
    TYPE_LIGHT: HaLight,
    TYPE_FAN: HaFan,
    TYPE_SWITCH: HaSwitch,
    TYPE_COVER: HaCover,
    TYPE_CLIMATE: HaClimate,
    TYPE_HUMIDIFIER: HaHumidifier,
    TYPE_VACUUM: HaVacuum,
}


def build_devices_from_spec(client: HassClient, spec: List[Dict[str, Any]]) -> List[Device]:
    """按白名单规格构建 HA 设备；未知类型跳过并告警（绝不自动暴露未列出实体）。"""
    devices: List[Device] = []
    for item in spec:
        device_type = str(item.get("type", "")).strip()
        cls = _DEVICE_TYPES.get(device_type)
        if cls is None:
            logging.getLogger("butler.ha").warning(
                "跳过不支持的设备类型 %r（entity=%s）", device_type, item.get("entity_id")
            )
            continue
        info = DeviceInfo(
            id=str(item["entity_id"]),
            name=str(item.get("name", item["entity_id"])),
            device_type=device_type,
            area=str(item.get("area", "未分区")),
        )
        devices.append(cls(client, info))
    return devices


def get_ha_client(config) -> HassClient:
    """按配置构造 HA 客户端（webapp/CLI/发现刷新共用同一实例）。"""
    return HassClient(
        base_url=config.ha_url,
        token=config.ha_token,
        timeout_seconds=config.ha_timeout_seconds,
    )


def build_ha_registry(config, client: Optional[HassClient] = None) -> "DeviceRegistry":
    """构建 HA 设备注册表。

    默认（DISCOVERY_ENABLED=1，阶段 14.5）：运行时自动发现——枚举 HA 全部实体，
    按域归类适配器、按能力位标志推导动作；高风险域与车库门结构性排除；
    用户排除表 data/discovery_exclusions.json 生效。
    DISCOVERY_ENABLED=0：回退旧白名单文件（config/ha_devices.json），行为不变。
    """
    from .registry import DeviceRegistry

    client = client or get_ha_client(config)
    if getattr(config, "discovery_enabled", True):
        from .discovery import discover_registry, load_exclusions

        exclusions_path = Path(config.data_dir) / "discovery_exclusions.json"
        registry, specs, skipped = discover_registry(
            client, load_exclusions(exclusions_path)
        )
        if skipped:
            logging.getLogger("butler.ha").warning(
                "自动发现跳过 %d 个实体（示例：%s）", len(skipped), skipped[:5]
            )
        if not specs:
            raise DeviceError(
                "自动发现未找到任何可管控设备：请确认 HA 已有 light/fan/switch/"
                "cover/climate/humidifier/vacuum/sensor 实体，且 .env 的 HA_URL/"
                "HA_TOKEN 有效（高风险域 lock/alarm/siren/water_heater 与车库门"
                "始终被结构性排除）。"
            )
        return registry

    devices_file = Path(config.ha_devices_file)
    if not devices_file.exists():
        raise DeviceError(
            f"找不到 HA 设备白名单文件：{devices_file}。"
            "请复制 config/ha_devices.example.json 为 config/ha_devices.json 并按需修改；"
            "或删除 .env 中的 DISCOVERY_ENABLED=0 恢复自动发现。"
        )
    spec = json.loads(devices_file.read_text(encoding="utf-8")).get("devices", [])
    registry = DeviceRegistry()
    for device in build_devices_from_spec(client, spec):
        registry.register(device)
    if not registry.list():
        raise DeviceError("HA 设备白名单为空：请至少在 config/ha_devices.json 配置一个设备")
    return registry
