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
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

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
        audit_sink=None,
        logger: logging.Logger = None,
        event_listener: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._registry = registry
        self._confirmer = confirmer
        self._confirmation_enabled = confirmation_enabled
        self._dedup_window = max(0.0, float(dedup_window_seconds))
        # 审计落点（可选）：每次工具执行后回调 audit_sink(action, detail, status)。
        # 传 memory_store.add_audit 即落库；不传则只记运行日志。
        self._audit = audit_sink
        self._event_listener = event_listener
        self._recent_success: Dict[str, Tuple[float, ToolResult]] = {}
        self._pool = futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="butler-tool")
        self._log = logger or logging.getLogger("butler.tools")

    def set_event_listener(self, listener: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """注册/替换安全流水线实时事件监听器（供 Web SSE / 观测面板使用）。"""
        self._event_listener = listener

    def _emit(
        self,
        trace_id: str,
        tool_name: str,
        step: str,
        status: str,
        detail: str = "",
        actor: str = "agent",
    ) -> None:
        listener = self._event_listener
        if listener is None:
            return
        try:
            listener({
                "trace_id": trace_id,
                "tool": tool_name,
                "step": step,
                "status": status,
                "detail": detail,
                "actor": actor,
            })
        except Exception:
            self._log.debug("流水线事件回调异常（已忽略）", exc_info=True)

    def execute_direct(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        actor: str = "web_direct",
    ) -> ToolResult:
        """直控入口（Web/App/小程序按钮）：跳过交互确认，保留全部校验/风控/防重放/回读/审计。"""
        raw = json.dumps(arguments, ensure_ascii=False)
        return self.execute(tool_name, raw, skip_confirmation=True, actor=actor)

    def execute(
        self,
        tool_name: str,
        raw_arguments: str,
        *,
        skip_confirmation: bool = False,
        actor: str = "agent",
    ) -> ToolResult:
        started = time.monotonic()
        trace_id = uuid.uuid4().hex[:10]
        result = self._execute_inner(
            tool_name,
            raw_arguments,
            trace_id=trace_id,
            skip_confirmation=skip_confirmation,
            actor=actor,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log.info(
            "工具执行 tool=%s actor=%s status=%s stage=%s duration_ms=%s args=%s",
            tool_name, actor, result.status, result.stage, elapsed_ms, (raw_arguments or "")[:200],
        )
        audit_action = "device.direct" if skip_confirmation else "tool.call"
        if self._audit is not None:
            try:
                self._audit(
                    audit_action, f"{tool_name} {(raw_arguments or '')[:200]}", result.status,
                    actor=actor,
                )
            except Exception:  # 审计失败不影响工具结果
                self._log.exception("审计记录写入失败 tool=%s", tool_name)
        self._emit(
            trace_id,
            tool_name,
            "verify_audit",
            "ok" if result.status == STATUS_OK else ("warn" if result.status == STATUS_DECLINED else "error"),
            f"status={result.status} stage={result.stage} ({elapsed_ms}ms)",
            actor=actor,
        )
        return result

    # ------------------------------------------------------------------ 主流程

    def _execute_inner(
        self,
        tool_name: str,
        raw_arguments: str,
        *,
        trace_id: str,
        skip_confirmation: bool = False,
        actor: str = "agent",
    ) -> ToolResult:
        spec = self._registry.get(tool_name)
        if spec is None:
            self._emit(trace_id, tool_name, "register_check", "error", "未注册工具", actor)
            return ToolResult(
                STATUS_ERROR,
                f"工具 {tool_name!r} 未注册，只能使用已注册工具",
                {"registered_tools": self._registry.names()},
                STAGE_FAILED,
            )
        self._emit(trace_id, tool_name, "register_check", "ok", "已注册", actor)

        try:
            args = json.loads(raw_arguments) if raw_arguments and raw_arguments.strip() else {}
        except ValueError as exc:
            self._emit(trace_id, tool_name, "json_parse", "error", str(exc), actor)
            return ToolResult(STATUS_ERROR, f"工具参数不是有效 JSON：{exc}", {}, STAGE_FAILED)
        if not isinstance(args, dict):
            self._emit(trace_id, tool_name, "json_parse", "error", "参数非 JSON 对象", actor)
            return ToolResult(STATUS_ERROR, "工具参数必须是 JSON 对象", {}, STAGE_FAILED)
        self._emit(trace_id, tool_name, "json_parse", "ok", "JSON 合法", actor)

        errors = validate_arguments(args, spec.parameters)
        if errors:
            msg = "；".join(errors)
            self._emit(trace_id, tool_name, "schema_validate", "error", msg, actor)
            return ToolResult(STATUS_ERROR, "参数校验失败：" + msg, {}, STAGE_FAILED)
        self._emit(trace_id, tool_name, "schema_validate", "ok", "Schema 校验通过", actor)

        if spec.risk == RISK_HIGH:
            self._emit(trace_id, tool_name, "risk_check", "error", "高风险域禁止自动执行", actor)
            return ToolResult(
                STATUS_ERROR,
                "高风险操作默认禁止自动执行；如确有需要，需单独设计授权流程",
                {},
                STAGE_FAILED,
            )
        self._emit(trace_id, tool_name, "risk_check", "ok", f"风险等级={spec.risk}", actor)

        # 防重放：非幂等控制操作在窗口期内的重复成功请求不再次执行
        dedup_key = self._dedup_key(spec, args)
        if spec.risk == RISK_MEDIUM and self._dedup_window > 0:
            cached = self._recent_success.get(dedup_key)
            if cached and (time.monotonic() - cached[0]) <= self._dedup_window:
                self._prune()
                previous = cached[1]
                self._emit(trace_id, tool_name, "dedup_check", "warn", "命中防重放窗口，跳过重复执行", actor)
                return ToolResult(
                    STATUS_OK,
                    f"{self._dedup_window:g} 秒内已执行过完全相同的请求，为防重复执行已跳过。"
                    f"上次结果：{previous.message}",
                    previous.data,
                    STAGE_DUPLICATE_SKIPPED,
                )
        self._emit(trace_id, tool_name, "dedup_check", "ok", "无重复冲突", actor)

        if spec.requires_confirmation():
            if skip_confirmation:
                self._emit(trace_id, tool_name, "user_confirm", "skipped", f"直控豁免（{actor}）", actor)
            elif not self._confirmation_enabled:
                self._log.warning(
                    "确认机制已被配置关闭（仅限调试环境），自动执行 %s", tool_name
                )
                self._emit(trace_id, tool_name, "user_confirm", "warn", "调试模式跳过确认", actor)
            else:
                summary = self._describe(spec, args)
                self._emit(trace_id, tool_name, "user_confirm", "pending", f"等待确认：{summary}", actor)
                approved = self._confirmer.confirm(summary, args)
                if not approved:
                    self._emit(trace_id, tool_name, "user_confirm", "error", "用户已拒绝", actor)
                    return ToolResult(
                        STATUS_DECLINED, "用户拒绝执行该操作，未对任何设备进行更改", {},
                        STAGE_CANCELLED,
                    )
                self._emit(trace_id, tool_name, "user_confirm", "ok", "用户已批准", actor)
        else:
            self._emit(trace_id, tool_name, "user_confirm", "skipped", "只读操作免确认", actor)

        self._emit(trace_id, tool_name, "execute", "pending", "执行设备/工具逻辑", actor)
        result = self._run_with_policy(spec, args)
        self._emit(
            trace_id,
            tool_name,
            "execute",
            "ok" if result.status == STATUS_OK else "error",
            result.message[:120],
            actor,
        )

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
        # 批量登记类工具（家庭食材/衣物等）：确认界面必须能看到具体清单，
        # 否则用户对着一个工具名确认就是在盲签
        items = args.get("items") if isinstance(args, dict) else None
        if isinstance(items, list) and items:
            names = [
                str(i.get("name", "?")) if isinstance(i, dict) else str(i) for i in items
            ]
            shown = "、".join(names[:8]) + ("…" if len(names) > 8 else "")
            return f"调用工具 {spec.name}（共 {len(items)} 项：{shown}）"
        return f"调用工具 {spec.name}"
