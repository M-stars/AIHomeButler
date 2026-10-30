"""每日 token 预算（评审 4.5）：按天累计模型用量，超限拒绝新的模型调用。

用量持久化在 memory.db 的 token_usage 表（按本地日期分桶），重启不清零；
预算为 0 表示不限额。强制点是"调用前检查"：已经发出的那一次调用无法回收，
超限当次用量照实入账，但下一次调用会被拒绝。
"""
from __future__ import annotations

import logging
from typing import Optional

from .llm_client import LLMError
from .memory.store import MemoryStore

_log = logging.getLogger("butler.budget")


class TokenBudget:
    def __init__(self, memory_store: MemoryStore, daily_limit: int,
                 logger: logging.Logger = None) -> None:
        self._store = memory_store
        self._limit = max(0, int(daily_limit))
        self._log = logger or _log

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def raise_if_exhausted(self) -> None:
        """预算已用尽时抛 LLMError（Agent/Planner 会收敛为可读回复，不崩溃）。"""
        if not self.enabled:
            return
        used = self._store.tokens_used_today()
        if used >= self._limit:
            raise LLMError(
                f"今日 token 预算已用尽（{used}/{self._limit}），模型调用已暂停。"
                "可调大 .env 中的 TOKEN_BUDGET_PER_DAY 或等待明天自动恢复。"
            )

    def add(self, total_tokens: int) -> int:
        """入账一次调用的用量，返回累计后的当日总量；越过预算线时告警一次。"""
        total_tokens = int(total_tokens or 0)
        if total_tokens <= 0:
            return self._store.tokens_used_today() if self.enabled else 0
        used = self._store.add_tokens_used(total_tokens)
        if self.enabled and used >= self._limit:
            self._log.warning("今日 token 用量已达预算线：%s/%s", used, self._limit)
        return used

    def status(self) -> dict:
        used = self._store.tokens_used_today() if self.enabled else 0
        return {
            "enabled": self.enabled,
            "limit": self._limit,
            "used_today": used,
            "remaining": max(0, self._limit - used),
        }
