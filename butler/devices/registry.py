"""设备注册表：统一按 ID 查询，上层不感知设备实现。"""
from __future__ import annotations

from typing import Dict, List

from .base import Device, DeviceError, DeviceNotFound, TYPE_SENSOR


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: Dict[str, Device] = {}

    def register(self, device: Device) -> None:
        if device.id in self._devices:
            raise DeviceError(f"设备重复注册：{device.id}")
        self._devices[device.id] = device

    def get(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFound(
                f"设备不存在：{device_id!r}（可先用 list_devices 查询有效设备）"
            ) from None

    def list(self) -> List[Device]:
        return list(self._devices.values())

    def by_type(self, device_type: str) -> List[Device]:
        return [d for d in self._devices.values() if d.info.device_type == device_type]

    def sensors(self) -> List[Device]:
        return self.by_type(TYPE_SENSOR)
