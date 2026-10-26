"""真实 API 集成测试：默认跳过，需设置 BUTLER_RUN_REAL_API_TESTS=1 才运行。

用途：验证与真实模型服务的全链路连通性（含工具调用与状态回读）。
不作为常规回归的一部分，避免成本与不确定性；常规回归全部离线。
"""
import os

import pytest

from butler.agent import Agent, build_system_prompt
from butler.config import ConfigError, load_config
from butler.conversation import Conversation
from butler.confirmation import AutoApproveConfirmer
from butler.llm_client import OpenAICompatClient
from butler.tools.executor import ToolExecutor
from butler.tools.home import build_default_devices, build_home_tools
from butler.tools.registry import ToolRegistry

pytestmark = pytest.mark.skipif(
    not os.environ.get("BUTLER_RUN_REAL_API_TESTS"),
    reason="设置 BUTLER_RUN_REAL_API_TESTS=1 才运行真实 API 集成测试",
)


def _build_agent(tmp_path):
    config = load_config()  # 读取项目根目录 .env
    registry = build_default_devices(tmp_path)  # 状态写入临时目录，不污染 data/
    tool_registry = ToolRegistry()
    for spec in build_home_tools(registry):
        tool_registry.register(spec)
    executor = ToolExecutor(tool_registry, AutoApproveConfirmer())
    llm = OpenAICompatClient(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        timeout_seconds=config.llm_timeout_seconds,
        max_retries=config.llm_max_retries,
    )
    agent = Agent(
        llm=llm,
        conversation=Conversation(build_system_prompt("（集成测试）", __import__("datetime").datetime.now()), 60),
        tool_registry=tool_registry,
        tool_executor=executor,
        max_tool_calls_per_turn=config.max_tool_calls_per_turn,
    )
    return agent


def test_real_api_sensor_query(tmp_path):
    try:
        agent = _build_agent(tmp_path)
    except ConfigError as exc:
        pytest.skip(f"缺少配置：{exc}")
    result = agent.process("现在客厅温度是多少度？")
    assert result.reply
    assert any(event.status == "ok" for event in result.tool_events)
