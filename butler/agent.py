"""Agent 核心：单 Agent + 工具调用循环 + 次数上限保护 + 异常收敛。

设计原则（阶段 1）：不引入多 Agent 与编排框架，官方 SDK 的 function calling
配合本循环与 ToolExecutor 已满足需求。阶段 6 任务规划在此基础上演进。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

from .conversation import Conversation
from .llm_client import LLMClient, LLMError
from .tools.executor import ToolExecutor
from .tools.registry import ToolRegistry

_log = logging.getLogger("butler.agent")


@dataclass
class ToolEvent:
    tool: str
    status: str
    message: str


@dataclass
class AgentTurnResult:
    reply: str
    tool_events: List[ToolEvent] = field(default_factory=list)
    stopped_by_limit: bool = False


def build_system_prompt(
    device_inventory: str,
    now: datetime,
    memory_block: str = "（暂无已保存的长期记忆）",
    member: dict = None,
) -> str:
    # member：声纹识别出的当前说话人（None=未识别/文本模式，行为不变）
    member_block = ""
    if member:
        member_block = (
            f"当前说话人：{member.get('display_name')}（家庭成员，经声纹识别）。"
            f"请用「{member.get('display_name')}」称呼对方；"
            "记忆中标注【成员名】的是 TA 的个人记忆，【共享】为全家共用，"
            "回答涉及偏好时优先采用 TA 的个人记忆。" + chr(10)
        )
    return f"""你是"AI Home Butler"家庭的 AI 管家（Butler），通过调用工具了解和操作家庭设备。
当前时间：{now.strftime('%Y-%m-%d %H:%M')}。
{member_block}
家庭设备清单：
{device_inventory}

用户长期记忆（由用户确认保存，可随时修改或删除）：
{memory_block}

规则：
1. 传感器数值、设备状态必须通过工具获取，禁止编造；工具没有的数据就如实说没有。
2. 控制设备（开/关/调节）前确保设备与参数正确；用户拒绝确认时，不得再次尝试，
   也不得声称操作已执行。
3. 只能使用已注册的工具；工具返回错误或状态未知时，如实向用户说明，不得宣称成功。
4. 只有当用户明确陈述稳定的偏好或事实并希望建议记住时，才调用 save_memory，
   且内容必须是用户原意；禁止把你的推测存为记忆。
5. 回答使用简体中文，简洁自然；执行操作后报告结果与设备当前状态。
"""


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        conversation: Conversation,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        max_tool_calls_per_turn: int = 8,
        system_prompt_provider=None,
        logger: logging.Logger = None,
    ) -> None:
        self._llm = llm
        self._conv = conversation
        self._registry = tool_registry
        self._executor = tool_executor
        self._max_calls = max(1, int(max_tool_calls_per_turn))
        self._prompt_provider = system_prompt_provider
        self._log = logger or _log

    def process(self, user_text: str) -> AgentTurnResult:
        """处理一轮用户输入。任何异常都收敛为可读回复，不崩溃、不死循环。"""
        if self._prompt_provider is not None:
            # 每轮刷新系统提示词，让最新记忆立即参与决策
            self._conv.update_system_prompt(self._prompt_provider())
        self._conv.add_user(user_text)
        events: List[ToolEvent] = []

        for _ in range(self._max_calls):
            try:
                response = self._llm.chat(
                    self._conv.to_api(), tools=self._registry.openai_schemas()
                )
            except LLMError as exc:
                return self._fail(events, f"模型服务暂时不可用：{exc}")

            if response.tool_calls:
                self._conv.add_assistant_tool_calls(response.content or "", response.tool_calls)
                for tool_call in response.tool_calls:
                    result = self._executor.execute(tool_call.name, tool_call.arguments)
                    events.append(ToolEvent(tool_call.name, result.status, result.message))
                    self._conv.add_tool_result(tool_call.id, result.to_payload())
                continue

            reply = response.content or "（模型未返回内容，请重试或换个说法）"
            self._conv.add_assistant_text(reply)
            return AgentTurnResult(reply=reply, tool_events=events)

        # 工具调用次数达到上限：注入收敛指令并做一次无工具的最终回答，防止无限循环
        self._log.warning("单轮工具调用达到上限 %s，强制收敛", self._max_calls)
        self._conv.add_user(
            "[系统] 已达单轮工具调用上限，请立即基于以上信息直接回答用户，不要再调用任何工具。"
        )
        try:
            response = self._llm.chat(self._conv.to_api(), tools=None)
            reply = response.content or "任务较为复杂，已中止本轮工具调用，请简化需求后重试。"
        except LLMError:
            reply = "已达单轮工具调用上限，且模型服务暂时不可用，本轮任务中止。"
        self._conv.add_assistant_text(reply)
        return AgentTurnResult(reply=reply, tool_events=events, stopped_by_limit=True)

    def _fail(self, events: List[ToolEvent], message: str) -> AgentTurnResult:
        self._log.error("Agent 轮次失败：%s", message)
        self._conv.add_assistant_text(message)
        return AgentTurnResult(reply=message, tool_events=events)
