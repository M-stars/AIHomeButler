"""V2 升级回归测试：HA 熔断器、设备直控 API、App/小程序多端配对、人设设置、安全流水线事件与前端静态资源。"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from butler.agent import Agent, build_system_prompt
from butler.conversation import Conversation
from butler.devices.base import DeviceOffline
from butler.devices.homeassistant import CircuitBreaker, HassClient
from butler.memory.store import MemoryStore
from butler.proactive import RuleEngine
from butler.tools.executor import ToolExecutor
from butler.webapp import WebConfirmer, create_app
from helpers import FakeLLM

_AUTH = {"X-Butler-Token": "primary-secret-token"}


class _Cfg:
    model = "test-model"
    base_url = "https://api.example.com"
    device_backend = "simulated"
    confirmation_mode = "always"
    web_cors_origins = "*"


@pytest.fixture
def app_ctx(tmp_path, registry, tool_registry):
    memory = MemoryStore(tmp_path / "v2_memory.db")
    confirmer = WebConfirmer(timeout_seconds=5)
    executor = ToolExecutor(tool_registry, confirmer, audit_sink=memory.add_audit)
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
        auth_token="primary-secret-token",
        executor=executor,
    )
    return {
        "app": app,
        "memory": memory,
        "registry": registry,
        "executor": executor,
        "confirmer": confirmer,
    }


# ------------------------------------------------------------------ HA 熔断器


def test_circuit_breaker_opens_and_fast_fails(monkeypatch):
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("butler.devices.homeassistant.httpx.request", _boom)
    client = HassClient(
        base_url="http://127.0.0.1:9",
        token="t",
        timeout_seconds=1.0,
        circuit_failure_threshold=2,
        circuit_recovery_timeout=60.0,
    )
    # 前 2 次传输失败触发熔断
    for _ in range(2):
        with pytest.raises(DeviceOffline):
            client.get_state("light.a")
    assert calls["n"] == 2
    assert client.circuit_breaker_status()["state"] == CircuitBreaker.STATE_OPEN

    # 第 3 次应直接快速失败，不再发底层网络请求
    with pytest.raises(DeviceOffline) as excinfo:
        client.get_state("light.a")
    assert "熔断" in str(excinfo.value)
    assert calls["n"] == 2


def test_circuit_breaker_recovers_after_timeout(monkeypatch):
    state = {"fail": True}

    class _OkResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"state": "on", "attributes": {}}

    def _mock_req(*args, **kwargs):
        if state["fail"]:
            raise httpx.ConnectTimeout("timeout")
        return _OkResp()

    monkeypatch.setattr("butler.devices.homeassistant.httpx.request", _mock_req)
    client = HassClient(
        base_url="http://127.0.0.1:9",
        token="t",
        timeout_seconds=1.0,
        circuit_failure_threshold=1,
        circuit_recovery_timeout=0.15,
    )
    with pytest.raises(DeviceOffline):
        client.get_state("light.a")
    assert client.circuit_breaker_status()["state"] == CircuitBreaker.STATE_OPEN

    time.sleep(0.2)
    state["fail"] = False
    res = client.get_state("light.a")
    assert res["state"] == "on"
    assert client.circuit_breaker_status()["state"] == CircuitBreaker.STATE_CLOSED


# --------------------------------------------------------------- 公开探针与静态页


def test_public_health_and_frontend_assets(app_ctx):
    anon = TestClient(app_ctx["app"])
    health = anon.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["protocol_version"] == "1.0"

    assert anon.get("/").status_code == 200
    assert anon.get("/manifest.json").status_code == 200
    assert anon.get("/static/styles.css").status_code == 200
    assert anon.get("/static/app.js").status_code == 200


# --------------------------------------------------------------- 设备直控 API


def test_direct_device_command_bypasses_confirmation_and_audits(app_ctx):
    client = TestClient(app_ctx["app"], headers=_AUTH)
    resp = client.post(
        "/api/devices/light.living_room/command",
        json={"action": "turn_on"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["verified"] is True
    assert body["state_after"]["on"] is True

    # 单设备状态接口
    st = client.get("/api/devices/light.living_room/state").json()
    assert st["state"]["on"] is True

    # 传感器不可控 → 403
    sensor_resp = client.post(
        "/api/devices/sensor.living_room/command",
        json={"action": "turn_on"},
    )
    assert sensor_resp.status_code == 403

    # 审计日志区分 actor=web_direct
    audits = client.get("/api/audit").json()["audit"]
    assert any(a["actor"] == "web_direct" and a["action"] == "device.direct" for a in audits)


# ------------------------------------------------------- App / 小程序配对协议


def test_pairing_lifecycle_and_multi_token_auth(app_ctx):
    primary = TestClient(app_ctx["app"], headers=_AUTH)
    anon = TestClient(app_ctx["app"])

    # 1. 发起配对获取 6 位码
    start = primary.post("/api/pair/start").json()
    code = start["code"]
    assert len(code) == 6 and code.isdigit()

    # 2. 错误配对码被拒绝
    bad = anon.post(
        "/api/pair/confirm",
        json={"code": "000000" if code != "000000" else "999999", "device_name": "测试小程序", "platform": "miniprogram"},
    )
    assert bad.status_code == 400

    # 3. 正确配对码换取设备令牌
    paired = anon.post(
        "/api/pair/confirm",
        json={"code": code, "device_name": "我的微信小程序", "platform": "miniprogram"},
    )
    assert paired.status_code == 200
    p_data = paired.json()
    dev_id = p_data["device_id"]
    dev_token = p_data["token"]
    assert dev_token.startswith("bh_")

    # 4. 一次性消费：重复使用同一配对码失败
    reuse = anon.post(
        "/api/pair/confirm",
        json={"code": code, "device_name": "重放攻击", "platform": "android"},
    )
    assert reuse.status_code == 400

    # 5. 使用 Bearer 设备令牌访问 API 并直控设备
    mp_client = TestClient(app_ctx["app"], headers={"Authorization": f"Bearer {dev_token}"})
    assert mp_client.get("/api/system/info").status_code == 200
    cmd = mp_client.post(
        "/api/devices/light.living_room/command",
        json={"action": "turn_on"},
    )
    assert cmd.status_code == 200 and cmd.json()["verified"] is True

    # 审计记录应标注 device_app:<device_id>
    audits = primary.get("/api/audit").json()["audit"]
    assert any(a["actor"] == f"device_app:{dev_id}" for a in audits)

    # 6. 非主令牌不能发起新配对或踢除设备
    assert mp_client.post("/api/pair/start").status_code == 403
    assert mp_client.delete(f"/api/pair/devices/{dev_id}").status_code == 403

    # 7. 主令牌踢除设备 → 设备令牌立即失效
    assert primary.delete(f"/api/pair/devices/{dev_id}").json()["revoked"] is True
    assert mp_client.get("/api/overview").status_code == 401


# ------------------------------------------------------- 人设与安全流水线事件


def test_persona_settings_and_prompt_injection(app_ctx):
    client = TestClient(app_ctx["app"], headers=_AUTH)
    init = client.get("/api/settings/persona").json()
    assert init["persona"] == ""
    assert len(init["presets"]) >= 3

    saved = client.put("/api/settings/persona", json={"persona": "简洁友好，每次汇报带温度"}).json()
    assert saved["saved"] is True
    assert client.get("/api/settings/persona").json()["persona"] == "简洁友好，每次汇报带温度"

    from datetime import datetime
    prompt = build_system_prompt("  - light.a", datetime.now(), persona="简洁友好")
    assert "简洁友好" in prompt
    assert "不得覆盖下方安全规则" in prompt


def test_executor_emits_pipeline_steps(app_ctx):
    captured = []
    app_ctx["executor"].set_event_listener(captured.append)
    res = app_ctx["executor"].execute_direct(
        "control_device", {"device_id": "light.living_room", "action": "turn_on"}, actor="web_direct"
    )
    assert res.status == "ok"
    steps = [e["step"] for e in captured]
    for expected in (
        "register_check",
        "json_parse",
        "schema_validate",
        "risk_check",
        "dedup_check",
        "user_confirm",
        "execute",
        "verify_audit",
    ):
        assert expected in steps


def test_cli_deliver_notifications_handles_exception_without_name_error():
    from butler import cli

    class _BrokenEngine:
        def run_checks(self):
            raise RuntimeError("boom")

    # 此前 cli.py 缺少 import logging 会在此处抛出 NameError
    cli._deliver_notifications(_BrokenEngine())
