"""工具执行流水线 —— 安全层核心（阶段 4 强化版）。

执行顺序：
  注册检查 → JSON 解析 → Schema 校验 → 风险等级 → 防重放 → 用户确认
  → 带超时执行（只读工具自动重试瞬时错误）→ 结果阶段标记 → 审计日志

保证：
  - 未注册工具、非法参数、高风险操作：一律拒绝，异常不逃逸；
  - 非幂等控制操作：3 秒内的重复成功请求直接返回上次结果，绝不重复执行；
  - 超时不等于失败：结果标记为 failed 并注明"操作可能仍在后台进行"；
  - 确认被拒绝时操作绝不执行。
安全约束由代码保证，不依赖系统提示词。
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from ..confirmation import Confirmer
from ..devices.base import DeviceError, StateUnknownError
from .base import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    STAGE_CANCELLED,
    STAGE_DONE,
    STAGE_DUPLICATE_SKIPPED,
    STAGE_FAILED,
    STAGE_VERIFIED,
    STATUS_DECLINED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNKNOWN,
    ToolResult,
    ToolSpec,
    ToolTimeoutError,
)
from .registry import ToolRegistry
from .validation import validate_arguments

_MAX_READ_RETRIES = 2
_RETRY_BACKOFF_SECONDS = (0.5, 1.0)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        confirmer: Confirmer,
        confirmation_enabled: bool = True,
        dedup_window_seconds: float = 3.0,
        logger: logging.Logger = None,
    ) -> None:
        self._registry = registry
        self._confirmer = confirmer
        self._confirmation_enabled = confirmation_enabled
        self._dedup_window = max(0.0, float(dedup_window_seconds))
        self._recent_success: Dict[str, Tuple[float, ToolResult]] = {}
        self._pool = futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="butler-tool")
        self._log = logger or logging.getLogger("butler.tools")

    def execute(self, tool_name: str, raw_arguments: str) -> ToolResult:
        started = time.monotonic()
        result = self._execute_inner(tool_name, raw_arguments)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log.info(
            "工具执行 tool=%s status=%s stage=%s duration_ms=%s args=%s",
            tool_name, result.status, result.stage, elapsed_ms, (raw_arguments or "")[:200],
        )
        return result

    # ------------------------------------------------------------------ 主流程

    def _execute_inner(self, tool_name: str, raw_arguments: str) -> ToolResult:
        spec = self._registry.get(tool_name)
        if spec is None:
            return ToolResult(
                STATUS_ERROR,
                f"工具 {tool_name!r} 未注册，只能使用已注册工具",
                {"registered_tools": self._registry.names()},
                STAGE_FAILED,
            )

        try:
            args = json.loads(raw_arguments) if raw_arguments and raw_arguments.strip() else {}
        except ValueError as exc:
            return ToolResult(STATUS_ERROR, f"工具参数不是有效 JSON：{exc}", {}, STAGE_FAILED)
        if not isinstance(args, dict):
            return ToolResult(STATUS_ERROR, "工具参数必须是 JSON 对象", {}, STAGE_FAILED)

        errors = validate_arguments(args, spec.parameters)
        if errors:
            return ToolResult(STATUS_ERROR, "参数校验失败：" + "；".join(errors), {}, STAGE_FAILED)

        if spec.risk == RISK_HIGH:
            return ToolResult(
                STATUS_ERROR,
                "高风险操作默认禁止自动执行；如确有需要，需单独设计授权流程",
                {},
                STAGE_FAILED,
            )

        # 防重放：非幂等控制操作在窗口期内的重复成功请求不再次执行
        dedup_key = self._dedup_key(spec, args)
        if spec.risk == RISK_MEDIUM and self._dedup_window > 0:
            cached = self._recent_success.get(dedup_key)
            if cached and (time.monotonic() - cached[0]) <= self._dedup_window:
                self._prune()
                previous = cached[1]
                return ToolResult(
                    STATUS_OK,
                    f"{self._dedup_window:g} 秒内已执行过完全相同的请求，为防重复执行已跳过。"
                    f"上次结果：{previous.message}",
                    previous.data,
                    STAGE_DUPLICATE_SKIPPED,
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
                        STATUS_DECLINED, "用户拒绝执行该操作，未对任何设备进行更改", {},
                        STAGE_CANCELLED,
                    )

        result = self._run_with_policy(spec, args)

        if result.status == STATUS_OK and spec.risk == RISK_MEDIUM and self._dedup_window > 0:
            self._recent_success[dedup_key] = (time.monotonic(), result)
            self._prune()
        return result

    # ------------------------------------------------------------ 执行与重试

    def _run_with_policy(self, spec: ToolSpec, args: Dict) -> ToolResult:
        """带超时执行；只读工具对瞬时离线错误自动重试，控制工具绝不自动重试。

        安全不变量由代码强制：即使工具被误配 retryable=True，非 LOW 风险的
        操作也不会自动重试（防止重复执行非幂等操作）。
        """
        max_attempts = 1 + (_MAX_READ_RETRIES if spec.retryable and spec.risk == RISK_LOW else 0)
        last_offline: Optional[str] = None
        for attempt in range(max_attempts):
            try:
                data = self._run_with_timeout(spec, args)
            except ToolTimeoutError as exc:
                return ToolResult(
                    STATUS_ERROR,
                    f"{exc}。注意：操作可能仍在后台进行，其结果未经验证，不得视为成功",
                    {},
                    STAGE_FAILED,
                )
            except StateUnknownError as exc:
                return ToolResult(STATUS_UNKNOWN, f"状态未知：{exc}", {}, "unknown")
            except DeviceError as exc:
                if spec.retryable and attempt < max_attempts - 1:
                    last_offline = str(exc)
                    self._log.warning(
                        "工具 %s 第 %s 次尝试失败，准备重试：%s", spec.name, attempt + 1, exc
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
                    continue
                return ToolResult(STATUS_ERROR, f"设备操作失败：{exc}", {}, STAGE_FAILED)
            except Exception as exc:  # 工具内部错误不能让 Agent 崩溃
                self._log.exception("工具 %s 内部错误", spec.name)
                return ToolResult(STATUS_ERROR, f"工具内部错误：{type(exc).__name__}", {}, STAGE_FAILED)

            stage = STAGE_VERIFIED if data.get("verified") is True else STAGE_DONE
            return ToolResult(STATUS_OK, "执行成功", data, stage)
        return ToolResult(STATUS_ERROR, f"设备操作失败：{last_offline}", {}, STAGE_FAILED)

    def _run_with_timeout(self, spec: ToolSpec, args: Dict) -> Dict:
        if spec.timeout_seconds and spec.timeout_seconds > 0:
            future = self._pool.submit(spec.handler, args)
            try:
                return future.result(timeout=spec.timeout_seconds)
            except futures.TimeoutError:
                raise ToolTimeoutError(
                    f"工具 {spec.name} 执行超时（>{spec.timeout_seconds:g}s）"
                ) from None
        return spec.handler(args)

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _dedup_key(spec: ToolSpec, args: Dict) -> str:
        return spec.name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._dedup_window
        stale = [key for key, (ts, _) in self._recent_success.items() if ts < cutoff]
        for key in stale:
            self._recent_success.pop(key, None)

    @staticmethod
    def _describe(spec: ToolSpec, args: Any) -> str:
        if spec.name == "control_device":
            value_part = f"（{args.get('value')}）" if args.get("value") is not None else ""
            return f"控制设备 {args.get('device_id')}：{args.get('action')}{value_part}"
        return f"调用工具 {spec.name}"
