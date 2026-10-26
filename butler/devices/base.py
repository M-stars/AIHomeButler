"""设备抽象。

上层（工具、Agent）只依赖本模块的接口与异常语义，不关心后端是模拟设备、
Home Assistant 还是 ESPHome。阶段 2/3 通过新增适配器替换实现。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List

TYPE_SENSOR = "sensor"
TYPE_LIGHT = "light"
TYPE_FAN = "fan"


class DeviceError(Exception):
    """设备操作失败（含参数非法、动作不支持等）。message 面向模型/用户可读。"""


class DeviceNotFound(DeviceError):
    """设备 ID 不存在。"""


class DeviceOffline(DeviceError):
    """设备离线或不可用。"""


class StateUnknownError(Exception):
    """操作可能已执行，但状态无法确认。不得报告为成功，也不得报告为失败。"""


@dataclass
class DeviceInfo:
    id: str
    name: str
    device_type: str
    area: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.device_type,
            "area": self.area,
        }


class Device(ABC):
    """所有设备的统一接口：可查询可用性与当前状态。"""

    def __init__(self, info: DeviceInfo) -> None:
        self.info = info

    @property
    def id(self) -> str:
        return self.info.id

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def read_state(self) -> Dict:
        """读取当前状态。设备离线时抛 DeviceOffline。"""


class ControllableDevice(Device):
    """支持控制指令的设备。实现方负责校验 action/value 的合法性。"""

    @abstractmethod
    def supported_actions(self) -> List[str]:
        ...

    @abstractmethod
    def apply(self, action: str, value=None) -> Dict:
        """执行动作并返回执行后的状态。非法动作/参数抛 DeviceError。"""
