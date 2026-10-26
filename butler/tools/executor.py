"""工具执行流水线 —— 安全层核心。

执行顺序：
  注册检查 → JSON 解析 → Schema 校验 → 风险等级检查 → 用户确认 → 执行 → 审计日志

任何一步失败都返回结构化 ToolResult，异常不会逃逸到 Agent 循环；
确认被拒绝时操作绝不执行。安全约束由代码保证，不依赖系统提示词。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..confirmation import Confirmer
from ..devices.base import DeviceError, StateUnknownError
from .base import RISK_HIGH, STATUS_DECLINED, STATUS_ERROR, STATUS_OK, STATUS_UNKNOWN, ToolResult, ToolSpec
from .registry import ToolRegistry
from .validation import validate_arguments


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        confirmer: Confirmer,
        confirmation_enabled: bool = True,
        logger: logging.Logger = None,
    ) -> None:
        self._registry = registry
        self._confirmer = confirmer
        self._confirmation_enabled = confirmation_enabled
        self._log = logger or logging.getLogger("butler.tools")

    def execute(self, tool_name: str, raw_arguments: str) -> ToolResult:
        started = time.monotonic()
        result = self._execute_inner(tool_name, raw_arguments)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log.info(
            "工具执行 tool=%s status=%s duration_ms=%s args=%s",
            tool_name, result.status, elapsed_ms, (raw_arguments or "")[:200],
        )
        return result

    def _execute_inner(self, tool_name: str, raw_arguments: str) -> ToolResult:
        spec = self._registry.get(tool_name)
        if spec is None:
            return ToolResult(
                STATUS_ERROR,
                f"工具 {tool_name!r} 未注册，只能使用已注册工具",
                {"registered_tools": self._registry.names()},
            )

        try:
            args = json.loads(raw_arguments) if raw_arguments and raw_arguments.strip() else {}
        except ValueError as exc:
            return ToolResult(STATUS_ERROR, f"工具参数不是有效 JSON：{exc}")
        if not isinstance(args, dict):
            return ToolResult(STATUS_ERROR, "工具参数必须是 JSON 对象")

        errors = validate_arguments(args, spec.parameters)
        if errors:
            return ToolResult(STATUS_ERROR, "参数校验失败：" + "；".join(errors))

        if spec.risk == RISK_HIGH:
            return ToolResult(
                STATUS_ERROR,
                "高风险操作默认禁止自动执行；如确有需要，需单独设计授权流程",
            )

        if spec.requires_confirmation():
            if not self._confirmation_enabled:
                self._log.warning(
                    "确认机制已被配置关闭（仅限调试环境），自动执行 %s", tool_name
                )
            else:
                approved = self._confirmer.confirm(self._describe(spec, args), args)
                if not approved:
                    return ToolResult(
                        STATUS_DECLINED, "用户拒绝执行该操作，未对任何设备进行更改"
                    )

        try:
            data = spec.handler(args)
        except StateUnknownError as exc:
            return ToolResult(STATUS_UNKNOWN, f"状态未知：{exc}")
        except DeviceError as exc:
            return ToolResult(STATUS_ERROR, f"设备操作失败：{exc}")
        except Exception as exc:  # 工具内部错误不能让 Agent 崩溃
            self._log.exception("工具 %s 内部错误", tool_name)
            return ToolResult(STATUS_ERROR, f"工具内部错误：{type(exc).__name__}")

        return ToolResult(STATUS_OK, "执行成功", data)

    @staticmethod
    def _describe(spec: ToolSpec, args: Any) -> str:
        if spec.name == "control_device":
            value_part = f"（{args.get('value')}）" if args.get("value") is not None else ""
            return f"控制设备 {args.get('device_id')}：{args.get('action')}{value_part}"
        return f"调用工具 {spec.name}"
