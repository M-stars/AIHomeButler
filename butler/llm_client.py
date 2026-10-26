"""LLM 访问适配层。

任何兼容 OpenAI Chat Completions 接口的服务（DeepSeek、Moonshot、OpenAI、
本地 vLLM 等）都通过 .env 的 base_url/model/key 接入，替换厂商不改代码。

职责：统一消息格式、透明重试（网络/超时/限流/服务端 5xx）、把 SDK 异常收敛为
LLMError、记录 Token 用量。不承担任何业务逻辑。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

_log = logging.getLogger("butler.llm")

_RETRYABLE_ERRORS = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
)


class LLMError(Exception):
    """模型调用最终失败（重试耗尽或不适合重试）。message 可直接展示给用户。"""


@dataclass
class ToolCall:
    """模型发起的一次工具调用（arguments 为原始 JSON 字符串）。"""

    id: str
    name: str
    arguments: str


@dataclass
class LLMResponse:
    content: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    def chat(self, messages: List[dict], tools: Optional[List[dict]] = None) -> LLMResponse:
        ...


class OpenAICompatClient:
    """OpenAI 兼容接口客户端（DeepSeek 官方推荐的接入方式）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise LLMError("API Key 为空，请检查 .env 中的 LLM_API_KEY")
        # SDK 自带重试关闭，由本类统一控制退避节奏
        self._client = OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_seconds, max_retries=0
        )
        self._model = model
        self._max_retries = max(1, int(max_retries))

    def chat(self, messages: List[dict], tools: Optional[List[dict]] = None) -> LLMResponse:
        kwargs: Dict = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.chat.completions.create(**kwargs)
                return self._parse(response)
            except _RETRYABLE_ERRORS as exc:  # 网络/超时/限流/5xx：退避后重试
                last_error = exc
                wait_seconds = 2 ** (attempt - 1)
                _log.warning(
                    "LLM 调用失败（第 %s/%s 次），%ss 后重试：%s",
                    attempt, self._max_retries, wait_seconds, type(exc).__name__,
                )
                time.sleep(wait_seconds)
            except APIStatusError as exc:  # 4xx 等：重试无意义
                raise LLMError(
                    f"模型服务返回错误（HTTP {exc.status_code}）：{str(exc)[:300]}"
                ) from exc
            except Exception as exc:  # 防御：SDK 其他异常也不能让主循环崩溃
                raise LLMError(f"模型调用出现意外错误：{type(exc).__name__}") from exc
        raise LLMError(
            f"模型服务在重试 {self._max_retries} 次后仍不可用：{type(last_error).__name__}"
        )

    def _parse(self, response) -> LLMResponse:
        message = response.choices[0].message if response.choices else None
        content = getattr(message, "content", None) if message is not None else None
        tool_calls: List[ToolCall] = []
        if message is not None:
            for tc in getattr(message, "tool_calls", None) or []:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments or "{}",
                    )
                )
        usage: Dict[str, int] = {}
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            usage = {
                "prompt_tokens": raw_usage.prompt_tokens,
                "completion_tokens": raw_usage.completion_tokens,
                "total_tokens": raw_usage.total_tokens,
            }
        _log.info(
            "LLM 响应 model=%s tool_calls=%s usage=%s", self._model, len(tool_calls), usage
        )
        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)
