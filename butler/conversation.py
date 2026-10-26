"""对话上下文管理。

裁剪策略：历史超过上限时按"整轮"（从一条 user 消息到下一条 user 消息之前）丢弃，
保证 assistant 的 tool_calls 与对应 tool 结果消息永远成对出现，不被切断。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Message:
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[dict]] = None  # 已按 API 格式组织
    tool_call_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None  # tool 消息的结构化内容


class Conversation:
    def __init__(self, system_prompt: str, max_messages: int = 60) -> None:
        self._messages: List[Message] = [Message(role="system", content=system_prompt)]
        self._max_messages = max(10, int(max_messages))

    @property
    def messages(self) -> List[Message]:
        return list(self._messages)

    def add_user(self, text: str) -> None:
        self._messages.append(Message(role="user", content=text))
        self._trim()

    def update_system_prompt(self, text: str) -> None:
        """刷新系统提示词（用于每轮注入最新记忆快照）。"""
        self._messages[0] = Message(role="system", content=text)

    def add_assistant_text(self, text: Optional[str]) -> None:
        self._messages.append(Message(role="assistant", content=text or ""))

    def add_assistant_tool_calls(self, content: Optional[str], tool_calls: List[Any]) -> None:
        formatted = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in tool_calls
        ]
        self._messages.append(
            Message(role="assistant", content=content or "", tool_calls=formatted)
        )

    def add_tool_result(self, tool_call_id: str, payload: Dict[str, Any]) -> None:
        self._messages.append(
            Message(role="tool", tool_call_id=tool_call_id, payload=payload)
        )

    def to_api(self) -> List[dict]:
        out: List[dict] = []
        for m in self._messages:
            if m.role in ("system", "user"):
                out.append({"role": m.role, "content": m.content or ""})
            elif m.role == "assistant":
                entry = {"role": "assistant", "content": m.content or ""}
                if m.tool_calls:
                    entry["tool_calls"] = m.tool_calls
                out.append(entry)
            elif m.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.tool_call_id,
                        "content": json.dumps(m.payload or {}, ensure_ascii=False),
                    }
                )
        return out

    def _trim(self) -> None:
        while (len(self._messages) - 1) > self._max_messages:
            first_user = next(
                (i for i, m in enumerate(self._messages) if i > 0 and m.role == "user"),
                None,
            )
            if first_user is None:
                return
            next_user = next(
                (
                    i
                    for i, m in enumerate(self._messages)
                    if i > first_user and m.role == "user"
                ),
                len(self._messages),
            )
            if next_user <= first_user:
                return
            del self._messages[first_user:next_user]
