"""阶段 5 记忆系统测试：CRUD、持久化、确认机制、提示词注入。"""
import json

import pytest

from butler.confirmation import AutoApproveConfirmer, AutoDenyConfirmer
from butler.memory.store import MemoryStore
from butler.tools.registry import ToolRegistry
from butler.tools.memory_tools import build_memory_tools
from helpers import make_executor


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


@pytest.fixture
def memory_tool_registry(store):
    registry = ToolRegistry()
    for spec in build_memory_tools(store):
        registry.register(spec)
    return registry


def test_memory_crud_roundtrip(store):
    memory_id = store.add("preference", "睡觉时关闭卧室灯")
    assert store.list()[0]["content"] == "睡觉时关闭卧室灯"
    assert store.list()[0]["source"] == "user"

    assert store.update(memory_id, "睡觉时关闭卧室灯并调暗客厅灯") is True
    memories = store.list()
    assert len(memories) == 1
    assert memories[0]["content"] == "睡觉时关闭卧室灯并调暗客厅灯"

    assert store.delete(memory_id) is True
    assert store.list() == []
    assert store.delete(memory_id) is False  # 重复删除返回 False 而非异常


def test_memory_rejects_invalid(store):
    from butler.memory.store import MemoryStore as _MS

    with pytest.raises(ValueError):
        store.add("gossip", "不存在的类型")
    with pytest.raises(ValueError):
        store.add("preference", "   ")


def test_memory_persists_across_reopen(tmp_path):
    db_path = tmp_path / "memory.db"
    store1 = MemoryStore(db_path)
    store1.add("fact", "家里有一只猫")
    store1.close()
    store2 = MemoryStore(db_path)
    memories = store2.list()
    assert len(memories) == 1
    assert memories[0]["content"] == "家里有一只猫"


def test_prompt_block_lists_memories(store):
    assert "暂无" in store.prompt_block()
    store.add("preference", "冬天空调不超过 24 度")
    block = store.prompt_block()
    assert "冬天空调不超过 24 度" in block
    assert "偏好" in block


def test_save_memory_requires_confirmation(memory_tool_registry, store):
    executor = make_executor(memory_tool_registry, AutoApproveConfirmer())
    result = executor.execute(
        "save_memory",
        json.dumps({"kind": "preference", "content": "回家先开客厅灯"}),
    )
    assert result.status == "ok"
    assert store.list()[0]["content"] == "回家先开客厅灯"


def test_save_memory_denied_means_not_saved(memory_tool_registry, store):
    executor = make_executor(memory_tool_registry, AutoDenyConfirmer())
    result = executor.execute(
        "save_memory",
        json.dumps({"kind": "preference", "content": "不该保存的内容"}),
    )
    assert result.status == "declined"
    assert store.list() == []  # 用户拒绝 → 绝不写入


def test_delete_nonexistent_memory_is_error(memory_tool_registry):
    executor = make_executor(memory_tool_registry, AutoApproveConfirmer())
    result = executor.execute("delete_memory", json.dumps({"memory_id": 999}))
    assert result.status == "error"
    assert "不存在" in result.message


def test_export_returns_everything(store):
    store.add("preference", "偏好A")
    store.add("fact", "事实B")
    store.add_task_log("测试任务", "completed", [{"index": 1}])
    data = store.export()
    assert data["memories"][0]["content"] in ("偏好A", "事实B")
    assert data["task_logs"][0]["outcome"] == "completed"


def test_agent_refreshes_system_prompt_with_latest_memory(tmp_path):
    """记忆保存后，下一轮系统提示词应立即包含新记忆。"""
    from butler.agent import Agent
    from butler.conversation import Conversation
    from helpers import FakeLLM, text_response, tool_call_response

    store = MemoryStore(tmp_path / "memory.db")
    registry = ToolRegistry()
    for spec in build_memory_tools(store):
        registry.register(spec)

    llm = FakeLLM(
        [
            tool_call_response(
                "save_memory",
                json.dumps({"kind": "preference", "content": "我习惯睡前喝温水"}),
                call_id="c1",
            ),
            text_response("已记住。"),
            text_response("好的。"),
        ]
    )
    conversation = Conversation("初始提示", 60)

    def prompt_provider():
        from butler.agent import build_system_prompt
        from datetime import datetime

        return build_system_prompt("设备", datetime.now(), store.prompt_block())

    agent = Agent(llm, conversation, registry, make_executor(registry), system_prompt_provider=prompt_provider)
    agent.process("记住我习惯睡前喝温水")
    agent.process("好的，请继续")

    # 第二轮的系统提示词应包含刚保存的记忆
    second_call_system = llm.calls[2]["messages"][0]["content"]
    assert "我习惯睡前喝温水" in second_call_system


def test_audit_records_memory_mutations(store):
    """记忆增/改/删自动落审计（评审 4.4），倒序返回最新在前。"""
    memory_id = store.add("preference", "喜欢柔和灯光")
    store.update(memory_id, "喜欢夜灯")
    store.delete(memory_id)
    entries = store.list_audit()
    assert [e["action"] for e in entries[:3]] == [
        "memory.delete", "memory.update", "memory.add",
    ]
    assert f"#{memory_id}" in entries[2]["detail"]


def test_audit_rejects_empty_action(store):
    import pytest as _pytest
    with _pytest.raises(ValueError):
        store.add_audit("  ")


def test_token_usage_accumulates_and_buckets_by_day(store):
    assert store.add_tokens_used(10) == 10
    assert store.add_tokens_used(15) == 25
    assert store.tokens_used_today() == 25
