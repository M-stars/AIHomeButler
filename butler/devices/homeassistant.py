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
    TYPE_FAN,
    TYPE_LIGHT,
    TYPE_SENSOR,
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


class _HaDeviceBase(Device):
    """HA 实体的公共封装：可用性判断 + 状态读取骨架。"""

    def __init__(self, client: HassClient, info: DeviceInfo) -> None:
        super().__init__(info)
        self._client = client

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
    """HA 灯实体：开关 + 亮度（HA 0-255 ↔ 本系统 1-100）。"""

    ACTIONS = ["turn_on", "turn_off", "set_brightness"]

    def supported_actions(self) -> List[str]:
        return list(self.ACTIONS)

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
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.ACTIONS)}"
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

    ACTIONS = ["turn_on", "turn_off", "set_speed"]

    def supported_actions(self) -> List[str]:
        return list(self.ACTIONS)

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
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.ACTIONS)}"
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


_DEVICE_TYPES = {TYPE_SENSOR: HaSensor, TYPE_LIGHT: HaLight, TYPE_FAN: HaFan}


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


def build_ha_registry(config) -> "DeviceRegistry":
    from .registry import DeviceRegistry

    devices_file = Path(config.ha_devices_file)
    if not devices_file.exists():
        raise DeviceError(
            f"找不到 HA 设备白名单文件：{devices_file}。"
            "请复制 config/ha_devices.example.json 为 config/ha_devices.json 并按需修改。"
        )
    spec = json.loads(devices_file.read_text(encoding="utf-8")).get("devices", [])
    client = HassClient(
        base_url=config.ha_url,
        token=config.ha_token,
        timeout_seconds=config.ha_timeout_seconds,
    )
    registry = DeviceRegistry()
    for device in build_devices_from_spec(client, spec):
        registry.register(device)
    if not registry.list():
        raise DeviceError("HA 设备白名单为空：请至少在 config/ha_devices.json 配置一个设备")
    return registry
