"""工具注册表：未注册的工具不可执行（白名单机制）。"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def openai_schemas(self) -> List[dict]:
        return [spec.openai_schema() for spec in self._tools.values()]
