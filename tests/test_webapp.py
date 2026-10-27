"""阶段 9 Web 界面测试：仪表盘数据、对话、确认队列、并发保护、记忆 API。"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from butler.agent import Agent
from butler.conversation import Conversation
from butler.memory.store import MemoryStore
from butler.proactive import RuleEngine
from butler.tools.executor import ToolExecutor
from butler.webapp import WebConfirmer, create_app
from helpers import FakeLLM, final_after_tool, text_response, tool_call_response


class _Cfg:
    model = "test-model"
    device_backend = "simulated"
    confirmation_mode = "always"


@pytest.fixture
def web(tmp_path, registry, tool_registry):
    memory = MemoryStore(tmp_path / "web_memory.db")
    confirmer = WebConfirmer(timeout_seconds=8)
    executor = ToolExecutor(tool_registry, confirmer)
    llm = FakeLLM([])
    agent = Agent(llm, Conversation("测试", 60), tool_registry, executor)
    engine = RuleEngine(registry, memory)
    app = create_app(
        config=_Cfg(),
        registry=registry,
        memory_store=memory,
        agent=agent,
        rule_engine=engine,
        confirmer=confirmer,
    )
    return {"app": app, "llm": llm, "memory": memory, "confirmer": confirmer}


def test_overview_lists_devices(web):
    client = TestClient(web["app"])
    data = client.get("/api/overview").json()
    ids = [d["id"] for d in data["devices"]]
    assert "light.living_room" in ids
    assert data["model"] == "test-model"


def test_chat_simple_text(web):
    web["llm"] and web["llm"]._steps.append(text_response("你好，我是管家。"))
    client = TestClient(web["app"])
    data = client.post("/api/chat", json={"text": "你好"}).json()
    assert data["reply"] == "你好，我是管家。"


def test_chat_control_flow_with_web_confirmation(web, registry):
    """对话触发控制 → 页面轮询到确认请求 → 批准 → 执行并验证。"""
    web["llm"]._steps.extend(
        [
            tool_call_response(
                "control_device",
                json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
                call_id="c1",
            ),
            final_after_tool(),
        ]
    )
    client = TestClient(web["app"])

    chat_result = {}

    def do_chat():
        resp = client.post("/api/chat", json={"text": "开客厅灯"})
        chat_result["data"] = resp.json()

    thread = threading.Thread(target=do_chat, daemon=True)
    thread.start()

    poller = TestClient(web["app"])
    deadline = time.time() + 6
    confirmations = []
    while time.time() < deadline:
        confirmations = poller.get("/api/confirmations").json()["confirmations"]
        if confirmations:
            break
        time.sleep(0.1)
    assert confirmations, "确认请求未在限时内出现"
    assert "turn_on" in json.dumps(confirmations[0]["arguments"])

    resp = poller.post(
        f"/api/confirmations/{confirmations[0]['id']}", json={"approved": True}
    )
    assert resp.json()["resolved"] is True

    thread.join(timeout=10)
    assert chat_result["data"]["reply"]
    assert registry.get("light.living_room").read_state()["on"] is True


def test_confirmation_rejection_blocks_execution(web, registry):
    web["llm"]._steps.extend(
        [
            tool_call_response(
                "control_device",
                json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
                call_id="c1",
            ),
            final_after_tool(expected_status="declined"),
        ]
    )
    client = TestClient(web["app"])
    chat_result = {}

    def do_chat():
        chat_result["data"] = client.post("/api/chat", json={"text": "开灯"}).json()

    thread = threading.Thread(target=do_chat, daemon=True)
    thread.start()
    poller = TestClient(web["app"])
    deadline = time.time() + 6
    confirmations = []
    while time.time() < deadline:
        confirmations = poller.get("/api/confirmations").json()["confirmations"]
        if confirmations:
            break
        time.sleep(0.1)
    poller.post(
        f"/api/confirmations/{confirmations[0]['id']}", json={"approved": False}
    )
    thread.join(timeout=10)

    events = chat_result["data"]["tool_events"]
    assert events and events[0]["status"] == "declined"
    assert registry.get("light.living_room").read_state()["on"] is False


def test_concurrent_chat_returns_409(web):
    def slow_step(messages):
        time.sleep(0.6)
        return text_response("慢慢回答")

    web["llm"]._steps.append(slow_step)
    client = TestClient(web["app"])
    first = {}
    thread = threading.Thread(
        target=lambda: first.update(resp=client.post("/api/chat", json={"text": "第一句"})),
        daemon=True,
    )
    thread.start()
    time.sleep(0.15)
    second = client.post("/api/chat", json={"text": "第二句"})
    assert second.status_code == 409
    thread.join(timeout=10)
    assert first["resp"].json()["reply"] == "慢慢回答"


def test_memory_api_crud(web):
    client = TestClient(web["app"])
    created = client.post(
        "/api/memories", json={"kind": "preference", "content": "周末晚起"}
    ).json()
    assert created["saved"] is True

    memories = client.get("/api/memories").json()["memories"]
    assert any(m["content"] == "周末晚起" for m in memories)

    assert client.delete(f"/api/memories/{created['id']}").json()["deleted"] is True
    assert client.delete(f"/api/memories/{created['id']}").status_code == 404


def test_notifications_endpoint_lists_stored_notifications(web, registry):
    """通知由后台调度器落库；端点只读（避免轮询反复触发检查）。"""
    web["memory"].add_rule(
        name="离线提醒", kind="device_offline", params={"device_id": "light.living_room"}
    )
    registry.get("light.living_room").set_unavailable(True)
    web["memory"].add_notification(1, "离线提醒", "设备 客厅灯（light.living_room）处于离线状态", "warning")
    client = TestClient(web["app"])
    data = client.get("/api/notifications").json()
    assert any("离线" in n["message"] for n in data["notifications"])
