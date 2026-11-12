"""设备注册表：统一按 ID 查询，上层不感知设备实现。

线程安全（阶段 14.5）：自动发现刷新线程会整体替换设备集合，而工具执行器/
Web 端点随时读写——所有访问走同一把锁。replace_all 是原子换血：要么全部
生效要么抛错保持原状，绝不允许"一半新一半旧"的中间态。
"""
from __future__ import annotations

import threading
from typing import Dict, List, Iterable

from .base import Device, DeviceError, DeviceNotFound, TYPE_SENSOR


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: Dict[str, Device] = {}
        self._lock = threading.RLock()

    def register(self, device: Device) -> None:
        with self._lock:
            if device.id in self._devices:
                raise DeviceError(f"设备重复注册：{device.id}")
            self._devices[device.id] = device

    def replace_all(self, devices: Iterable[Device]) -> None:
        """原子替换全部设备（自动发现刷新用）：批内重复 ID 或空集则抛错不变更。"""
        batch: Dict[str, Device] = {}
        for device in devices:
            if device.id in batch:
                raise DeviceError(f"设备重复注册：{device.id}")
            batch[device.id] = device
        if not batch:
            raise DeviceError("拒绝以空设备集替换注册表（疑似发现异常，保持现状）")
        with self._lock:
            self._devices = batch

    def get(self, device_id: str) -> Device:
        with self._lock:
            try:
                return self._devices[device_id]
            except KeyError:
                raise DeviceNotFound(
                    f"设备不存在：{device_id!r}（可先用 list_devices 查询有效设备）"
                ) from None

    def list(self) -> List[Device]:
        with self._lock:
            return list(self._devices.values())

    def by_type(self, device_type: str) -> List[Device]:
        with self._lock:
            return [d for d in self._devices.values() if d.info.device_type == device_type]

    def sensors(self) -> List[Device]:
        return self.by_type(TYPE_SENSOR)
