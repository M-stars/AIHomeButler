"""设备自动发现（阶段 14.5）：运行时从 HA 动态枚举实体并推导能力，替代静态白名单。

设计要点：
  - HA 是局域网接入枢纽（WiFi/Zigbee/ESPHome/Matter 设备都汇聚于此），管家的
    "自动识别" = 动态枚举 HA /api/states + 按域自动归类适配器，不直接对接私有协议
    （不重复造 HA，项目哲学不变）；
  - 能力（支持哪些动作）从 HA 实体的 supported_features 位标志 + 属性自动推导，
    新设备进网即出现，无需改任何配置文件；
  - 安全边界不变且自动执行：lock/alarm_control_panel/siren/water_heater 等高风险域
    与 cover 的 garage_door/gate 类结构性**永不纳入**（不依赖用户自觉）；
  - 排除表 data/discovery_exclusions.json 由用户手动维护（阶段 15 应用内管理）；
  - 周期刷新：新设备自动加入、消失设备自动移出，变更落审计；HA 不可达时保留
    上次清单继续服务（绝不因刷新失败清空注册表）。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import (
    TYPE_CLIMATE,
    TYPE_COVER,
    TYPE_FAN,
    TYPE_HUMIDIFIER,
    TYPE_LIGHT,
    TYPE_SENSOR,
    TYPE_SWITCH,
    TYPE_VACUUM,
    Device,
    DeviceError,
    DeviceInfo,
)
from .homeassistant import (
    HaClimate,
    HaCover,
    HaFan,
    HaHumidifier,
    HaLight,
    HaSensor,
    HaSwitch,
    HaVacuum,
    HassClient,
    supported_actions_for,
)

_log = logging.getLogger("butler.discovery")

# ---------------------------------------------------------------- 纳入/排除策略

# 可管控域 → (适配器类, 系统设备类型)；sensor 只读（阈值/定时规则的输入源）
_DOMAIN_ADAPTERS = {
    "light": (HaLight, TYPE_LIGHT),
    "fan": (HaFan, TYPE_FAN),
    "switch": (HaSwitch, TYPE_SWITCH),
    "cover": (HaCover, TYPE_COVER),
    "climate": (HaClimate, TYPE_CLIMATE),
    "humidifier": (HaHumidifier, TYPE_HUMIDIFIER),
    "vacuum": (HaVacuum, TYPE_VACUUM),
    "sensor": (HaSensor, TYPE_SENSOR),
}

# 高风险域结构性排除：任何情况下不纳入注册表（与既有白名单哲学一致）
FORBIDDEN_DOMAINS = frozenset({"lock", "alarm_control_panel", "siren", "water_heater"})
# cover 中人身高风险类别：车库门/院门绝不由 AI/应用控制。
# HA 现代常量为 CoverDeviceClass.GARAGE="garage"/GATE="gate"；garage_door 为旧名，
# 一并保留以防第三方集成仍用旧值（真机实测 demo 车库门 device_class="garage"）。
FORBIDDEN_COVER_DEVICE_CLASSES = frozenset({"garage", "gate", "garage_door"})

# ---------------------------------------------------------------- 能力快照
# 能力推导（位标志镜像 + supported_actions_for）单一事实源在 homeassistant.py，
# 本模块只负责"枚举→筛选→快照→实例化→周期刷新"。
_FEATURE_ATTR_KEYS = (
    "supported_features", "supported_color_modes", "device_class",
    "hvac_modes", "min_humidity", "max_humidity", "humidity",
    "percentage", "percentage_step", "preset_modes", "current_position",
    "fan_speed", "temperature", "target_temp_high", "target_temp_low",
)


@dataclass
class DeviceSpec:
    """一台被发现实体的静态描述：身份 + 能力快照。"""

    entity_id: str
    domain: str
    name: str
    device_type: str
    features: Dict[str, Any]  # 传给适配器做能力推导的属性快照
    actions: List[str]  # 推导出的支持动作（sensor 为空）


def _features_of(attributes: Dict[str, Any]) -> Dict[str, Any]:
    return {k: attributes[k] for k in _FEATURE_ATTR_KEYS if k in attributes}


def classify_entities(
    states: List[Dict[str, Any]], exclusions: Optional[Set[str]] = None
) -> Tuple[List[DeviceSpec], List[Tuple[str, str]]]:
    """把 HA /api/states 原始列表分类为纳入清单与跳过清单（含原因，便于诊断）。"""
    excluded = exclusions or set()
    specs: List[DeviceSpec] = []
    skipped: List[Tuple[str, str]] = []
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if not entity_id:
            skipped.append((entity_id, "无实体 ID"))
            continue
        if domain in FORBIDDEN_DOMAINS:
            # 必须先于"域不在清单"判断：这些域故意不进适配器表，但排除原因要如实
            skipped.append((entity_id, "高风险域结构性排除"))
            continue
        if domain not in _DOMAIN_ADAPTERS:
            skipped.append((entity_id, "域不在可管控清单"))
            continue
        if entity_id in excluded:
            skipped.append((entity_id, "用户排除表"))
            continue
        attributes = state.get("attributes") or {}
        features = _features_of(attributes)
        if domain == "cover" and attributes.get("device_class") in FORBIDDEN_COVER_DEVICE_CLASSES:
            skipped.append((entity_id, "车库/院门结构性排除"))
            continue
        actions = supported_actions_for(domain, features)
        if domain != "sensor" and not actions:
            skipped.append((entity_id, "未推导出可控能力（supported_features 缺失或为空）"))
            continue
        adapter_cls, device_type = _DOMAIN_ADAPTERS[domain]
        specs.append(
            DeviceSpec(
                entity_id=entity_id,
                domain=domain,
                name=str(attributes.get("friendly_name") or entity_id),
                device_type=device_type,
                features=features,
                actions=actions or [],
            )
        )
    return specs, skipped


def build_devices(client: HassClient, specs: List[DeviceSpec]) -> List[Device]:
    """按能力快照实例化适配器。sensor 无能力快照也照常实例化（只读）。"""
    devices: List[Device] = []
    for spec in specs:
        info = DeviceInfo(id=spec.entity_id, name=spec.name, device_type=spec.device_type, area="")
        adapter_cls = _DOMAIN_ADAPTERS[spec.domain][0]
        if spec.domain == "sensor":
            devices.append(adapter_cls(client, info))
        else:
            devices.append(adapter_cls(client, info, features=dict(spec.features)))
    return devices


def load_exclusions(path: Path) -> Set[str]:
    """读取排除表；文件缺失/损坏都视为空集（绝不因配置问题炸掉发现）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    items = data.get("excluded", []) if isinstance(data, dict) else []
    return {str(x) for x in items if isinstance(x, str)}


def discover_registry(
    client: HassClient, exclusions: Optional[Set[str]] = None
) -> Tuple["object", List[DeviceSpec], List[Tuple[str, str]]]:
    """一次性发现：返回 (注册表, 纳入清单, 跳过清单)。"""
    from .registry import DeviceRegistry

    states = client.get_states()
    specs, skipped = classify_entities(states, exclusions)
    registry = DeviceRegistry()
    for device in build_devices(client, specs):
        registry.register(device)
    return registry, specs, skipped


class DiscoveryRefresher:
    """周期刷新注册表：新设备自动纳入、消失设备自动移出，变更落审计。

    HA 不可达/单次失败只记日志，注册表保持上一份可用清单（服务连续性优先）。
    """

    def __init__(
        self,
        client: HassClient,
        registry,
        interval_seconds: float,
        memory_store=None,
        exclusions_path: Optional[Path] = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._interval = max(60.0, float(interval_seconds))
        self._store = memory_store
        self._exclusions_path = exclusions_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="butler-discovery", daemon=True)
        self._thread.start()
        _log.info("设备自动发现已启动（每 %.0fs 刷新）", self._interval)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:
                _log.exception("设备自动发现刷新失败（保留现有设备清单）")
            self._stop.wait(self._interval)

    def refresh_once(self) -> Dict[str, List[str]]:
        """执行一次发现并与注册表比对；返回 {"added": [...], "removed": [...]}。"""
        exclusions = (
            load_exclusions(self._exclusions_path) if self._exclusions_path else set()
        )
        states = self._client.get_states()
        specs, _skipped = classify_entities(states, exclusions)
        devices = build_devices(self._client, specs)
        new_ids = {d.id for d in devices}
        old_ids = {d.id for d in self._registry.list()}
        added, removed = sorted(new_ids - old_ids), sorted(old_ids - new_ids)
        if not added and not removed:
            return {"added": [], "removed": []}
        self._registry.replace_all(devices)
        for entity_id in added:
            spec = next(s for s in specs if s.entity_id == entity_id)
            _log.info("发现新设备：%s（%s，动作：%s）", entity_id, spec.name, "/".join(spec.actions))
            if self._store is not None:
                self._store.add_audit(
                    "discovery.found",
                    f"{entity_id} {spec.name}（{spec.device_type}，动作：{'/'.join(spec.actions) or '只读'}）",
                    "ok",
                )
        for entity_id in removed:
            _log.info("设备已消失：%s", entity_id)
            if self._store is not None:
                self._store.add_audit("discovery.lost", entity_id, "ok")
        if self._store is not None:
            self._store.add_audit(
                "discovery.refresh",
                f"设备 {len(new_ids)} 台（新增 {len(added)}，移除 {len(removed)}）",
                "ok",
            )
        return {"added": added, "removed": removed}


def raise_discovery_empty(skipped: List[Tuple[str, str]]) -> None:
    if not skipped:
        return
    _log.warning("发现阶段跳过 %d 个实体（示例：%s）", len(skipped), skipped[:5])
