"""测试共享辅助：FakeLLM、响应构造器、组件工厂。全部离线，不触网。"""
import json

from butler.agent import Agent
from butler.confirmation import AutoApproveConfirmer
from butler.conversation import Conversation
from butler.devices.base import TYPE_FAN, TYPE_LIGHT, TYPE_SENSOR, DeviceInfo
from butler.devices.registry import DeviceRegistry
from butler.devices.simulated import (
    JsonStateStore,
    SimulatedFan,
    SimulatedLight,
    SimulatedSensor,
)
from butler.llm_client import LLMResponse, ToolCall
from butler.tools.executor import ToolExecutor
from butler.tools.home import build_home_tools
from butler.tools.registry import ToolRegistry


class FakeLLM:
    """脚本化 LLM：按顺序返回预设 LLMResponse，或抛出异常；记录每次收到的消息。"""

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self._steps:
            raise AssertionError("FakeLLM 步骤已耗尽")
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages)
        return step


def text_response(text):
    return LLMResponse(content=text, tool_calls=[], usage={})


def tool_call_response(name, arguments, call_id="call_1"):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage={},
    )


def final_after_tool(expected_status="ok"):
    """脚本步骤：断言上一轮已收到指定状态的工具结果，然后返回最终文本。"""

    def _step(messages):
        tool_messages = [m for m in messages if m["role"] == "tool"]
        assert tool_messages, "此轮应已包含工具结果消息"
        payload = json.loads(tool_messages[-1]["content"])
        assert payload["status"] == expected_status, payload
        return text_response("已通过工具确认：" + payload["message"])

    return _step


def make_registry(store):
    registry = DeviceRegistry()
    registry.register(
        SimulatedSensor(
            DeviceInfo("sensor.living_room", "客厅温湿度传感器", TYPE_SENSOR, "客厅"), store
        )
    )
    registry.register(
        SimulatedLight(DeviceInfo("light.living_room", "客厅灯", TYPE_LIGHT, "客厅"), store)
    )
    registry.register(
        SimulatedFan(DeviceInfo("fan.bedroom", "卧室风扇", TYPE_FAN, "卧室"), store)
    )
    return registry


def make_tool_registry(registry):
    tool_registry = ToolRegistry()
    for spec in build_home_tools(registry):
        tool_registry.register(spec)
    return tool_registry


def make_executor(tool_registry, confirmer=None, confirmation_enabled=True):
    return ToolExecutor(
        tool_registry, confirmer or AutoApproveConfirmer(), confirmation_enabled=confirmation_enabled
    )


def make_agent(fake_llm, tool_registry, confirmer=None, max_tool_calls=8,
               max_tool_calls_per_response=4):
    conversation = Conversation("你是测试管家，回答用中文。", max_messages=60)
    executor = make_executor(tool_registry, confirmer)
    return Agent(
        llm=fake_llm,
        conversation=conversation,
        tool_registry=tool_registry,
        tool_executor=executor,
        max_tool_calls_per_turn=max_tool_calls,
        max_tool_calls_per_response=max_tool_calls_per_response,
    )
