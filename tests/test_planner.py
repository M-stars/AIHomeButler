"""阶段 6 任务规划器测试：计划验证、顺序执行、中止语义、任务日志。"""
import json

from butler.confirmation import AutoApproveConfirmer, AutoDenyConfirmer
from butler.llm_client import LLMResponse
from butler.planner import PlanRunner
from helpers import FakeLLM, make_agent, make_executor


def _plan_response(steps, explanation=""):
    return LLMResponse(
        content=json.dumps({"steps": steps, "explanation": explanation}, ensure_ascii=False),
        tool_calls=[],
        usage={},
    )


def _text(text):
    return LLMResponse(content=text, tool_calls=[], usage={})


def _make_runner(llm, tool_registry, registry, memory=None, confirmer=None):
    executor = make_executor(tool_registry, confirmer or AutoApproveConfirmer())
    return PlanRunner(llm, tool_registry, executor, registry, memory)


def test_happy_path_plan_executes_in_order(tool_registry, registry):
    llm = FakeLLM(
        [
            _plan_response(
                [
                    {"tool": "get_sensor_readings", "args": {}, "reason": "先看环境"},
                    {"tool": "control_device", "args": {"device_id": "light.living_room", "action": "turn_on"}, "reason": "开灯"},
                ]
            ),
            _text("已开灯，一切正常。"),
        ]
    )
    runner = _make_runner(llm, tool_registry, registry)
    report = runner.run("开客厅灯")

    assert report["status"] == "completed"
    assert report["steps"][0]["status"] == "ok"
    assert report["steps"][1]["status"] == "ok"
    assert registry.get("light.living_room").read_state()["on"] is True
    assert report["summary"] == "已开灯，一切正常。"
    assert len(llm.calls) == 2  # 规划 + 汇报，没有多余调用


def test_plan_with_unregistered_tool_rejected_before_execution(tool_registry, registry):
    llm = FakeLLM(
        [
            _plan_response(
                [{"tool": "open_gate", "args": {}, "reason": "试图开门"}]
            ),
            _text("（不应被用于汇报）"),
        ]
    )
    runner = _make_runner(llm, tool_registry, registry)
    report = runner.run("打开大门")

    assert report["status"] == "failed"
    assert "未注册" in report["summary"]
    assert registry.get("light.living_room").read_state()["on"] is False
    assert len(llm.calls) == 1  # 汇报调用都不该发生


def test_plan_with_invalid_args_rejected_before_execution(tool_registry, registry):
    llm = FakeLLM(
        [
            _plan_response(
                [{"tool": "control_device", "args": {"device_id": "light.living_room"}, "reason": "缺 action"}]
            ),
        ]
    )
    runner = _make_runner(llm, tool_registry, registry)
    report = runner.run("开灯")

    assert report["status"] == "failed"
    assert "参数不合法" in report["summary"]
    assert registry.get("light.living_room").read_state()["on"] is False


def test_declined_step_aborts_remaining(tool_registry, registry):
    llm = FakeLLM(
        [
            _plan_response(
                [
                    {"tool": "control_device", "args": {"device_id": "light.living_room", "action": "turn_on"}, "reason": "开灯"},
                    {"tool": "control_device", "args": {"device_id": "fan.bedroom", "action": "turn_on"}, "reason": "开风扇"},
                ]
            ),
            _text("用户取消了。"),
        ]
    )
    runner = _make_runner(llm, tool_registry, registry, confirmer=AutoDenyConfirmer())
    report = runner.run("开灯和风扇")

    assert report["status"] == "aborted"
    assert report["steps"][0]["status"] == "declined"
    assert report["steps"][1]["status"] == "pending"  # 第二步未执行
    assert "中止" in report["steps"][1]["message"]
    assert registry.get("fan.bedroom").read_state()["on"] is False


def test_error_step_aborts_remaining(tool_registry, registry):
    llm = FakeLLM(
        [
            _plan_response(
                [
                    {"tool": "control_device", "args": {"device_id": "light.nowhere", "action": "turn_on"}, "reason": "不存在的设备"},
                    {"tool": "control_device", "args": {"device_id": "light.living_room", "action": "turn_on"}, "reason": "开灯"},
                ]
            ),
            _text("有一步失败了。"),
        ]
    )
    runner = _make_runner(llm, tool_registry, registry)
    report = runner.run("开灯")

    assert report["status"] == "aborted"
    assert report["steps"][0]["status"] == "error"
    assert report["steps"][1]["status"] == "pending"
    assert registry.get("light.living_room").read_state()["on"] is False


def test_invalid_json_twice_fails_gracefully(tool_registry, registry):
    llm = FakeLLM([_text("这不是JSON"), _text("这也不是JSON")])
    runner = _make_runner(llm, tool_registry, registry)
    report = runner.run("随便做什么")

    assert report["status"] == "failed"
    assert "规划失败" in report["summary"]


def test_task_log_recorded(tmp_path, tool_registry, registry):
    from butler.memory.store import MemoryStore

    memory = MemoryStore(tmp_path / "memory.db")
    llm = FakeLLM(
        [
            _plan_response(
                [{"tool": "control_device", "args": {"device_id": "light.living_room", "action": "turn_on"}, "reason": "开灯"}]
            ),
            _text("完成。"),
        ]
    )
    runner = _make_runner(llm, tool_registry, registry, memory=memory)
    runner.run("开灯")

    logs = memory.list_task_logs()
    assert len(logs) == 1
    assert logs[0]["outcome"] == "completed"
    assert logs[0]["steps"][0]["tool"] == "control_device"


def test_plan_step_cap_enforced(tool_registry, registry):
    many_steps = [
        {"tool": "get_device_state", "args": {"device_id": "light.living_room"}, "reason": "查询"}
        for _ in range(20)
    ]
    llm = FakeLLM([_plan_response(many_steps)])
    runner = _make_runner(llm, tool_registry, registry)
    report = runner.run("反复查询")

    assert report["status"] == "failed"
    assert "上限" in report["summary"]
