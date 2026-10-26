import json

from butler.confirmation import AutoApproveConfirmer, AutoDenyConfirmer
from butler.llm_client import LLMError
from helpers import FakeLLM, final_after_tool, make_agent, text_response, tool_call_response


def test_sensor_query_roundtrip(tool_registry):
    llm = FakeLLM(
        [
            tool_call_response("get_sensor_readings", "{}", call_id="c1"),
            final_after_tool(),
        ]
    )
    agent = make_agent(llm, tool_registry)
    result = agent.process("现在客厅温度和湿度是多少？")

    assert result.reply
    assert len(result.tool_events) == 1
    assert result.tool_events[0].tool == "get_sensor_readings"
    assert result.tool_events[0].status == "ok"
    assert len(llm.calls) == 2  # 一次工具调用 + 一次最终回答


def test_control_with_approve_turns_light_on(tool_registry, registry):
    llm = FakeLLM(
        [
            tool_call_response(
                "control_device",
                json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
                call_id="c1",
            ),
            final_after_tool(),
        ]
    )
    agent = make_agent(llm, tool_registry, confirmer=AutoApproveConfirmer())
    result = agent.process("把客厅灯打开")

    assert result.tool_events[0].status == "ok"
    assert registry.get("light.living_room").read_state()["on"] is True


def test_control_declined_keeps_state_and_informs_model(tool_registry, registry):
    llm = FakeLLM(
        [
            tool_call_response(
                "control_device",
                json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
                call_id="c1",
            ),
            final_after_tool(expected_status="declined"),
        ]
    )
    agent = make_agent(llm, tool_registry, confirmer=AutoDenyConfirmer())
    result = agent.process("把客厅灯打开")

    assert result.tool_events[0].status == "declined"
    assert registry.get("light.living_room").read_state()["on"] is False


def test_model_self_corrects_invalid_arguments(tool_registry, registry):
    llm = FakeLLM(
        [
            tool_call_response("control_device", "{}", call_id="c1"),  # 缺少必填参数
            tool_call_response(
                "control_device",
                json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
                call_id="c2",
            ),
            final_after_tool(),
        ]
    )
    agent = make_agent(llm, tool_registry, confirmer=AutoApproveConfirmer())
    result = agent.process("开客厅灯")

    assert result.tool_events[0].status == "error"  # 第一次参数被拒并回传原因
    assert result.tool_events[1].status == "ok"  # 模型自纠后成功
    assert registry.get("light.living_room").read_state()["on"] is True


def test_tool_call_limit_stops_infinite_loop(tool_registry):
    counter = {"n": 0}

    def always_tool_call(messages):
        counter["n"] += 1
        return tool_call_response("get_sensor_readings", "{}", call_id=f"c{counter['n']}")

    llm = FakeLLM([always_tool_call] * 20)
    agent = make_agent(llm, tool_registry, max_tool_calls=3)
    result = agent.process("把所有设备查个遍")

    assert result.stopped_by_limit is True
    assert len(llm.calls) == 4  # 3 次工具轮 + 1 次强制收敛
    assert result.reply  # 收敛回答非空


def test_llm_error_returns_friendly_message_no_crash(tool_registry):
    llm = FakeLLM([LLMError("模拟网络中断")])
    agent = make_agent(llm, tool_registry)
    result = agent.process("你好")

    assert "模型服务暂时不可用" in result.reply
    assert "模拟网络中断" in result.reply


def test_multi_turn_context_preserved(tool_registry):
    llm = FakeLLM(
        [
            tool_call_response(
                "get_device_state",
                json.dumps({"device_id": "light.living_room"}),
                call_id="c1",
            ),
            text_response("客厅灯目前是关着的。"),
            text_response("我刚才说过：客厅灯目前是关着的。"),
        ]
    )
    agent = make_agent(llm, tool_registry)
    first = agent.process("客厅灯开着吗？")
    agent.process("你刚才说什么？")

    assert "关" in first.reply
    third_call_messages = llm.calls[2]["messages"]
    contents = [m.get("content") or "" for m in third_call_messages]
    assert any("客厅灯目前是关着的" in c for c in contents)
