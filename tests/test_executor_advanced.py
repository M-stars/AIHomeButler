"""阶段 4 强化测试：超时、防重放、只读重试、结果阶段标记。"""
import time

import pytest

from butler.devices.base import DeviceOffline
from butler.tools.base import (
    RISK_LOW,
    RISK_MEDIUM,
    ToolSpec,
    ToolTimeoutError,
)
from butler.tools.executor import ToolExecutor
from butler.tools.registry import ToolRegistry
from butler.confirmation import AutoApproveConfirmer


def _registry_with(specs):
    reg = ToolRegistry()
    for spec in specs:
        reg.register(spec)
    return reg


def test_tool_timeout_returns_failed_not_success():
    def slow_handler(args):
        time.sleep(1.0)
        return {"ok": True}

    registry = _registry_with(
        [
            ToolSpec(
                name="slow_read",
                description="",
                parameters={"type": "object", "properties": {}},
                risk=RISK_LOW,
                handler=slow_handler,
                timeout_seconds=0.2,
            )
        ]
    )
    executor = ToolExecutor(registry, AutoApproveConfirmer())
    result = executor.execute("slow_read", "{}")
    assert result.status == "error"
    assert "超时" in result.message
    assert "不得视为成功" in result.message
    # 超时后执行器仍可继续服务后续请求
    assert executor.execute("slow_read", "{}").status == "error"


def test_duplicate_control_request_is_skipped():
    calls = {"n": 0}

    def control_handler(args):
        calls["n"] += 1
        return {"device_id": args["device_id"], "action": args["action"], "verified": True}

    registry = _registry_with(
        [
            ToolSpec(
                name="control_device",
                description="",
                parameters={"type": "object", "properties": {}},
                risk=RISK_MEDIUM,
                handler=control_handler,
            )
        ]
    )
    executor = ToolExecutor(registry, AutoApproveConfirmer())
    first = executor.execute("control_device", '{"device_id": "light.x", "action": "turn_on"}')
    assert first.status == "ok" and first.stage == "verified"
    second = executor.execute("control_device", '{"device_id": "light.x", "action": "turn_on"}')
    assert second.status == "ok"
    assert second.stage == "duplicate_skipped"
    assert calls["n"] == 1  # 只真正执行了一次
    # 参数不同则不受影响
    third = executor.execute("control_device", '{"device_id": "light.y", "action": "turn_on"}')
    assert third.stage == "verified"
    assert calls["n"] == 2


def test_duplicate_window_expiry_allows_reexecution():
    calls = {"n": 0}

    def control_handler(args):
        calls["n"] += 1
        return {"verified": True}

    registry = _registry_with(
        [
            ToolSpec(
                name="control_device",
                description="",
                parameters={"type": "object", "properties": {}},
                risk=RISK_MEDIUM,
                handler=control_handler,
            )
        ]
    )
    executor = ToolExecutor(registry, AutoApproveConfirmer(), dedup_window_seconds=0.1)
    executor.execute("control_device", '{"device_id": "light.x", "action": "turn_on"}')
    time.sleep(0.15)
    executor.execute("control_device", '{"device_id": "light.x", "action": "turn_on"}')
    assert calls["n"] == 2


def test_read_tool_retries_transient_offline():
    attempts = {"n": 0}

    def flaky_read(args):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise DeviceOffline("瞬时离线")
        return {"value": 1}

    registry = _registry_with(
        [
            ToolSpec(
                name="read_sensor",
                description="",
                parameters={"type": "object", "properties": {}},
                risk=RISK_LOW,
                handler=flaky_read,
                retryable=True,
            )
        ]
    )
    executor = ToolExecutor(registry, AutoApproveConfirmer())
    result = executor.execute("read_sensor", "{}")
    assert result.status == "ok"
    assert attempts["n"] == 2


def test_control_tool_never_auto_retries():
    attempts = {"n": 0}

    def flaky_control(args):
        attempts["n"] += 1
        raise DeviceOffline("离线")

    registry = _registry_with(
        [
            ToolSpec(
                name="control_device",
                description="",
                parameters={"type": "object", "properties": {}},
                risk=RISK_MEDIUM,
                handler=flaky_control,
                retryable=True,  # 即使误配了 retryable，控制类也不应重试
            )
        ]
    )
    executor = ToolExecutor(registry, AutoApproveConfirmer())
    result = executor.execute("control_device", "{}")
    assert result.status == "error"
    assert attempts["n"] == 1  # 一次就停，防止重复执行非幂等操作


def test_result_stages_are_distinguished():
    def read_ok(args):
        return {"value": 1}

    def control_ok(args):
        return {"verified": True}

    def boom(args):
        raise RuntimeError("boom")

    registry = _registry_with(
        [
            ToolSpec(name="read_tool", description="", parameters={"type": "object", "properties": {}},
                     risk=RISK_LOW, handler=read_ok),
            ToolSpec(name="control_ok", description="", parameters={"type": "object", "properties": {}},
                     risk=RISK_MEDIUM, handler=control_ok),
            ToolSpec(name="boom_tool", description="", parameters={"type": "object", "properties": {}},
                     risk=RISK_LOW, handler=boom),
        ]
    )
    executor = ToolExecutor(registry, AutoApproveConfirmer())
    assert executor.execute("read_tool", "{}").stage == "done"
    assert executor.execute("control_ok", "{}").stage == "verified"
    assert executor.execute("boom_tool", "{}").stage == "failed"
    declined = ToolExecutor(
        _registry_with(
            [
                ToolSpec(name="control_ok", description="", parameters={"type": "object", "properties": {}},
                         risk=RISK_MEDIUM, handler=control_ok),
            ]
        ),
        AutoApproveConfirmer(),
    )
    from butler.confirmation import AutoDenyConfirmer

    executor2 = ToolExecutor(
        _registry_with(
            [
                ToolSpec(name="control_ok", description="", parameters={"type": "object", "properties": {}},
                         risk=RISK_MEDIUM, handler=control_ok),
            ]
        ),
        AutoDenyConfirmer(),
    )
    assert executor2.execute("control_ok", "{}").stage == "cancelled"


def test_tool_timeout_error_type_exists():
    # ToolTimeoutError 是显式类型，供上层区分"超时"与普通失败
    assert issubclass(ToolTimeoutError, Exception)
