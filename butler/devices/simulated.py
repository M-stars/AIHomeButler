"""模拟设备：温湿度传感器、灯、风扇，状态持久化到 JSON 文件（重启不丢）。

只解决"开发与测试需要稳定设备后端"的问题；阶段 2 由 HomeAssistantAdapter 替换。
传感器读数带 sampled_at 时间戳，供上层判断数据新鲜度。
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .base import (
    ControllableDevice,
    Device,
    DeviceError,
    DeviceInfo,
    DeviceOffline,
)

_log = logging.getLogger("butler.devices")


class JsonStateStore:
    """极简 JSON 持久化（单进程使用）。文件损坏时重置为空并保留损坏原件。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, ValueError) as exc:
            bad_path = self._path.with_suffix(".corrupt.json")
            try:
                self._path.replace(bad_path)
            except OSError:
                bad_path = None
            _log.warning(
                "设备状态文件损坏，已重置为默认状态（损坏原件保留在 %s）：%s",
                bad_path.name if bad_path else "(未能保留)", exc,
            )

    def get(self, device_id: str, default: dict) -> dict:
        return dict(self._data.get(device_id, default))

    def set(self, device_id: str, state: dict) -> None:
        self._data[device_id] = dict(state)
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class SimulatedSensor(Device):
    """温湿度传感器：读数随机小幅漂移，模拟真实环境。"""

    def __init__(self, info: DeviceInfo, store: JsonStateStore, unavailable: bool = False) -> None:
        super().__init__(info)
        self._store = store
        self._unavailable = unavailable
        state = store.get(info.id, {"temperature_c": 22.0, "humidity_percent": 50.0})
        self._temperature = float(state.get("temperature_c", 22.0))
        self._humidity = float(state.get("humidity_percent", 50.0))

    def set_unavailable(self, flag: bool) -> None:
        """测试/故障演练用：模拟设备离线。"""
        self._unavailable = bool(flag)

    def is_available(self) -> bool:
        return not self._unavailable

    def read_state(self) -> Dict:
        if self._unavailable:
            raise DeviceOffline(f"{self.info.name}({self.info.id}) 离线，无法读取传感器")
        # 随机漂移并夹紧到传感器量程内
        self._temperature = min(35.0, max(10.0, self._temperature + random.uniform(-0.4, 0.4)))
        self._humidity = min(90.0, max(20.0, self._humidity + random.uniform(-1.5, 1.5)))
        state = {
            "temperature_c": round(self._temperature, 1),
            "humidity_percent": round(self._humidity, 1),
            "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "fresh": True,
        }
        self._store.set(
            self.info.id,
            {"temperature_c": self._temperature, "humidity_percent": self._humidity},
        )
        return state


class SimulatedLight(ControllableDevice):
    """灯：开关 + 亮度（1-100）。"""

    ACTIONS: List[str] = ["turn_on", "turn_off", "set_brightness"]

    def __init__(self, info: DeviceInfo, store: JsonStateStore, unavailable: bool = False) -> None:
        super().__init__(info)
        self._store = store
        self._unavailable = unavailable
        state = store.get(info.id, {"on": False, "brightness": 80})
        self._state = {"on": bool(state.get("on", False)), "brightness": int(state.get("brightness", 80))}

    def set_unavailable(self, flag: bool) -> None:
        self._unavailable = bool(flag)

    def is_available(self) -> bool:
        return not self._unavailable

    def read_state(self) -> Dict:
        if self._unavailable:
            raise DeviceOffline(f"{self.info.name}({self.info.id}) 离线")
        return dict(self._state)

    def supported_actions(self) -> List[str]:
        return list(self.ACTIONS)

    def apply(self, action: str, value=None) -> Dict:
        if self._unavailable:
            raise DeviceOffline(f"{self.info.name}({self.info.id}) 离线，无法执行 {action}")
        if action == "turn_on":
            self._state["on"] = True
            if value is not None:
                self._state["brightness"] = self._parse_brightness(value)
        elif action == "turn_off":
            self._state["on"] = False
        elif action == "set_brightness":
            self._state["brightness"] = self._parse_brightness(value)
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.ACTIONS)}"
            )
        self._store.set(self.info.id, self._state)
        return dict(self._state)

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


class SimulatedFan(ControllableDevice):
    """风扇：开关 + 档位（low/medium/high）。"""

    ACTIONS: List[str] = ["turn_on", "turn_off", "set_speed"]
    SPEEDS: List[str] = ["low", "medium", "high"]

    def __init__(self, info: DeviceInfo, store: JsonStateStore, unavailable: bool = False) -> None:
        super().__init__(info)
        self._store = store
        self._unavailable = unavailable
        state = store.get(info.id, {"on": False, "speed": "medium"})
        self._state = {"on": bool(state.get("on", False)), "speed": str(state.get("speed", "medium"))}

    def set_unavailable(self, flag: bool) -> None:
        self._unavailable = bool(flag)

    def is_available(self) -> bool:
        return not self._unavailable

    def read_state(self) -> Dict:
        if self._unavailable:
            raise DeviceOffline(f"{self.info.name}({self.info.id}) 离线")
        return dict(self._state)

    def supported_actions(self) -> List[str]:
        return list(self.ACTIONS)

    def apply(self, action: str, value=None) -> Dict:
        if self._unavailable:
            raise DeviceOffline(f"{self.info.name}({self.info.id}) 离线，无法执行 {action}")
        if action == "turn_on":
            self._state["on"] = True
            if value is not None:
                self._state["speed"] = self._parse_speed(value)
        elif action == "turn_off":
            self._state["on"] = False
        elif action == "set_speed":
            self._state["speed"] = self._parse_speed(value)
        else:
            raise DeviceError(
                f"{self.info.name} 不支持动作 {action!r}，支持：{', '.join(self.ACTIONS)}"
            )
        self._store.set(self.info.id, self._state)
        return dict(self._state)

    def _parse_speed(self, value) -> str:
        if not isinstance(value, str) or value not in self.SPEEDS:
            raise DeviceError(f"风速档位必须是 {'/'.join(self.SPEEDS)}")
        return value
