import json

from butler.confirmation import AutoApproveConfirmer, AutoDenyConfirmer
from butler.tools.base import RISK_HIGH, ToolSpec
from butler.tools.executor import ToolExecutor
from butler.tools.registry import ToolRegistry
from helpers import make_executor


def test_unknown_tool_rejected(tool_registry):
    executor = make_executor(tool_registry)
    result = executor.execute("no_such_tool", "{}")
    assert result.status == "error"
    assert "未注册" in result.message


def test_invalid_json_arguments(tool_registry):
    executor = make_executor(tool_registry)
    result = executor.execute("get_device_state", "{bad json")
    assert result.status == "error"
    assert "JSON" in result.message


def test_schema_violation_blocks_execution(tool_registry):
    executor = make_executor(tool_registry)
    result = executor.execute("get_device_state", "{}")
    assert result.status == "error"
    assert "device_id" in result.message


def test_low_risk_runs_without_asking_confirmation(tool_registry):
    # AutoDeny 作为 confirmer：LOW 风险不应触发确认，直接执行
    executor = ToolExecutor(tool_registry, AutoDenyConfirmer())
    result = executor.execute("list_devices", "{}")
    assert result.status == "ok"
    assert result.data["count"] >= 1


def test_control_executes_on_approve_and_verifies(tool_registry, registry):
    executor = make_executor(tool_registry, AutoApproveConfirmer())
    result = executor.execute(
        "control_device",
        json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
    )
    assert result.status == "ok"
    assert result.data["verified"] is True
    assert registry.get("light.living_room").read_state()["on"] is True


def test_decline_keeps_device_state(tool_registry, registry):
    executor = make_executor(tool_registry, AutoDenyConfirmer())
    result = executor.execute(
        "control_device",
        json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
    )
    assert result.status == "declined"
    assert "拒绝" in result.message
    assert registry.get("light.living_room").read_state()["on"] is False


def test_control_unknown_device_reports_error(tool_registry):
    executor = make_executor(tool_registry, AutoApproveConfirmer())
    result = executor.execute(
        "control_device",
        json.dumps({"device_id": "light.nowhere", "action": "turn_on"}),
    )
    assert result.status == "error"
    assert "不存在" in result.message


def test_control_offline_device_reports_error(tool_registry, registry):
    registry.get("light.living_room").set_unavailable(True)
    executor = make_executor(tool_registry, AutoApproveConfirmer())
    result = executor.execute(
        "control_device",
        json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
    )
    assert result.status == "error"
    assert "离线" in result.message


def test_control_missing_value_reports_error(tool_registry):
    executor = make_executor(tool_registry, AutoApproveConfirmer())
    result = executor.execute(
        "control_device",
        json.dumps({"device_id": "light.living_room", "action": "set_brightness"}),
    )
    assert result.status == "error"


def test_action_not_supported_by_device_type(tool_registry):
    executor = make_executor(tool_registry, AutoApproveConfirmer())
    result = executor.execute(
        "control_device",
        json.dumps({"device_id": "fan.bedroom", "action": "set_brightness", "value": 50}),
    )
    assert result.status == "error"
    assert "不支持" in result.message


def test_high_risk_tool_refused_even_if_confirmed():
    tool_registry = ToolRegistry()

    def handler(args):
        return {}

    tool_registry.register(
        ToolSpec(
            name="open_door",
            description="高风险示例",
            parameters={"type": "object", "properties": {}, "required": []},
            risk=RISK_HIGH,
            handler=handler,
        )
    )
    executor = ToolExecutor(tool_registry, AutoApproveConfirmer())
    result = executor.execute("open_door", "{}")
    assert result.status == "error"
    assert "高风险" in result.message


def test_disabled_confirmation_is_explicit_bypass(tool_registry, registry):
    # CONFIRMATION_MODE=never 的调试路径：跳过确认直接执行（启动时会输出警告日志）
    executor = ToolExecutor(tool_registry, AutoDenyConfirmer(), confirmation_enabled=False)
    result = executor.execute(
        "control_device",
        json.dumps({"device_id": "light.living_room", "action": "turn_on"}),
    )
    assert result.status == "ok"
