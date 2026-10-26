"""阶段 1 的家庭工具集：作用于设备注册表的查询与控制工具。

控制类 handler 遵循固定套路：
  预检查（存在 / 在线 / 动作合法）→ 执行 → 回读状态验证。
  回读失败 → StateUnknownError（状态未知）；验证不符 → DeviceError（如实失败）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from ..devices.base import (
    TYPE_FAN,
    TYPE_LIGHT,
    TYPE_SENSOR,
    ControllableDevice,
    Device,
    DeviceError,
    DeviceInfo,
    StateUnknownError,
)
from ..devices.registry import DeviceRegistry
from ..devices.simulated import (
    JsonStateStore,
    SimulatedFan,
    SimulatedLight,
    SimulatedSensor,
)
from .base import RISK_LOW, RISK_MEDIUM, ToolSpec


def build_default_devices(data_dir: Path) -> DeviceRegistry:
    """阶段 1 的默认模拟设备。阶段 2 由后端选择逻辑替换。"""
    store = JsonStateStore(Path(data_dir) / "simulated_state.json")
    registry = DeviceRegistry()
    registry.register(
        SimulatedSensor(
            DeviceInfo(
                id="sensor.living_room",
                name="客厅温湿度传感器",
                device_type=TYPE_SENSOR,
                area="客厅",
            ),
            store,
        )
    )
    registry.register(
        SimulatedLight(
            DeviceInfo(
                id="light.living_room", name="客厅灯", device_type=TYPE_LIGHT, area="客厅"
            ),
            store,
        )
    )
    registry.register(
        SimulatedLight(
            DeviceInfo(
                id="light.bedroom", name="卧室灯", device_type=TYPE_LIGHT, area="卧室"
            ),
            store,
        )
    )
    registry.register(
        SimulatedFan(
            DeviceInfo(
                id="fan.bedroom", name="卧室风扇", device_type=TYPE_FAN, area="卧室"
            ),
            store,
        )
    )
    return registry


def build_home_tools(registry: DeviceRegistry) -> List[ToolSpec]:
    def _list_devices(args: Dict) -> Dict:
        devices = [
            {
                "id": d.info.id,
                "name": d.info.name,
                "type": d.info.device_type,
                "area": d.info.area,
                "available": d.is_available(),
            }
            for d in registry.list()
        ]
        return {"devices": devices, "count": len(devices)}

    def _get_device_state(args: Dict) -> Dict:
        device = registry.get(args["device_id"])
        return {
            "device_id": device.info.id,
            "name": device.info.name,
            "type": device.info.device_type,
            "available": device.is_available(),
            "state": device.read_state(),
        }

    def _get_sensor_readings(args: Dict) -> Dict:
        readings = []
        for device in registry.by_type(TYPE_SENSOR):
            try:
                readings.append(
                    {
                        "sensor_id": device.info.id,
                        "name": device.info.name,
                        "area": device.info.area,
                        **device.read_state(),
                    }
                )
            except DeviceError as exc:
                readings.append(
                    {
                        "sensor_id": device.info.id,
                        "name": device.info.name,
                        "area": device.info.area,
                        "available": False,
                        "error": str(exc),
                    }
                )
        return {"readings": readings, "count": len(readings)}

    def _control_device(args: Dict) -> Dict:
        device = registry.get(args["device_id"])
        action = args["action"]
        value = args.get("value")
        if not isinstance(device, ControllableDevice):
            raise DeviceError(f"{device.info.name} 不是可控设备")
        if action not in device.supported_actions():
            raise DeviceError(
                f"{device.info.name} 不支持动作 {action!r}，"
                f"支持：{', '.join(device.supported_actions())}"
            )
        state_before = device.read_state()
        device.apply(action, value)
        try:
            state_after = device.read_state()
        except DeviceError as exc:
            raise StateUnknownError(
                f"{device.info.name} 的 {action} 指令可能已执行，但回读状态失败：{exc}"
            )
        if not _action_took_effect(action, value, state_after):
            raise DeviceError(f"执行后验证失败：{action} 执行后状态仍为 {state_after}")
        return {
            "device_id": device.info.id,
            "action": action,
            "value": value,
            "state_before": state_before,
            "state_after": state_after,
            "verified": True,
        }

    return [
        ToolSpec(
            name="list_devices",
            description="列出家庭中所有设备及其类型、区域、可用性。不确定设备 ID 时先调用它。",
            parameters={"type": "object", "properties": {}, "required": []},
            risk=RISK_LOW,
            handler=_list_devices,
        ),
        ToolSpec(
            name="get_device_state",
            description="查询单个设备的当前状态（传感器读数、灯开关/亮度、风扇开关/档位）。",
            parameters={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "设备 ID，例如 light.living_room",
                    }
                },
                "required": ["device_id"],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_get_device_state,
        ),
        ToolSpec(
            name="get_sensor_readings",
            description="一次性读取所有温湿度传感器的最新读数，包含采样时间。",
            parameters={"type": "object", "properties": {}, "required": []},
            risk=RISK_LOW,
            handler=_get_sensor_readings,
        ),
        ToolSpec(
            name="control_device",
            description=(
                "控制灯或风扇。灯支持 turn_on / turn_off / set_brightness(1-100)；"
                "风扇支持 turn_on / turn_off / set_speed(low|medium|high)。"
                "该操作需要用户确认后才会执行。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备 ID"},
                    "action": {
                        "type": "string",
                        "enum": ["turn_on", "turn_off", "set_brightness", "set_speed"],
                    },
                    "value": {
                        "description": (
                            "set_brightness 传 1-100 的整数；set_speed 传 low/medium/high；"
                            "其他动作不要传本参数"
                        )
                    },
                },
                "required": ["device_id", "action"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_control_device,
        ),
    ]


def _action_took_effect(action: str, value, state: Dict) -> bool:
    if action == "turn_on":
        return state.get("on") is True
    if action == "turn_off":
        return state.get("on") is False
    if action == "set_brightness":
        try:
            return int(state.get("brightness")) == int(value)
        except (TypeError, ValueError):
            return False
    if action == "set_speed":
        return state.get("speed") == value
    return True
