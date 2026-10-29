"""HA 适配器集成测试：在进程内启动 HA API 模拟器，走真实 HTTP 链路验证。"""
import httpx
import json
import socket
import threading
import time

import pytest
import uvicorn

from butler.devices.base import (
    DeviceNotFound,
    DeviceOffline,
    StateUnknownError,
    TYPE_FAN,
    TYPE_LIGHT,
    TYPE_SENSOR,
)
from butler.devices.homeassistant import (
    HassApiError,
    HassClient,
    build_devices_from_spec,
    build_ha_registry,
)
from butler.emulator.ha import create_emulator_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def emulator_url():
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_emulator_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "模拟器未能启动"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def client(emulator_url):
    return HassClient(base_url=emulator_url, token="test-token", timeout_seconds=5.0)


@pytest.fixture
def registry(client):
    devices = build_devices_from_spec(
        client,
        [
            {"entity_id": "sensor.living_room_temp", "name": "客厅温度", "type": "sensor", "area": "客厅"},
            {"entity_id": "light.living_room", "name": "客厅灯", "type": "light", "area": "客厅"},
            {"entity_id": "fan.bedroom", "name": "卧室风扇", "type": "fan", "area": "卧室"},
            {"entity_id": "light.broken", "name": "故障灯", "type": "light", "area": "客厅"},
        ],
    )
    reg = type(devices[0]).__mro__  # noqa: F841  仅占位避免误用
    from butler.devices.registry import DeviceRegistry

    result = DeviceRegistry()
    for device in devices:
        result.register(device)
    return result


def test_sensor_read_value_unit_freshness(registry):
    sensor = registry.get("sensor.living_room_temp")
    state = sensor.read_state()
    assert state["value"] == 22.5
    assert state["unit"] == "°C"
    assert state["fresh"] is True
    assert state["sampled_at"]


def test_light_control_roundtrip_with_brightness_mapping(registry):
    light = registry.get("light.living_room")
    assert light.read_state()["on"] is False
    light.apply("turn_on")
    assert light.read_state()["on"] is True
    light.apply("set_brightness", 50)
    state = light.read_state()
    assert state["on"] is True
    assert state["brightness"] == 50  # 255*50/100=128 → 回读 128*100/255≈50
    light.apply("turn_off")
    assert light.read_state()["on"] is False


def test_fan_speed_roundtrip(registry):
    fan = registry.get("fan.bedroom")
    fan.apply("turn_on", "high")
    state = fan.read_state()
    assert state["on"] is True and state["speed"] == "high"
    fan.apply("set_speed", "low")
    assert fan.read_state()["speed"] == "low"
    fan.apply("turn_off")
    assert fan.read_state()["on"] is False


def test_offline_entity_reports_offline(registry):
    broken = registry.get("light.broken")
    assert broken.is_available() is False
    with pytest.raises(DeviceOffline):
        broken.read_state()


def test_unknown_entity_maps_to_not_found(client):
    with pytest.raises(DeviceNotFound):
        client.get_state("light.nowhere")


def test_bad_token_maps_to_api_error(emulator_url):
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_emulator_app(require_token="right-token"),
            host="127.0.0.1", port=port, log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    try:
        bad = HassClient(base_url=f"http://127.0.0.1:{port}", token="wrong", timeout_seconds=5.0)
        with pytest.raises(HassApiError) as excinfo:
            bad.get_state("light.living_room")
        assert "401" in str(excinfo.value) and "HA_TOKEN" in str(excinfo.value)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _transport_always_fails(exc: Exception):
    """让 httpx 传输层必然抛出给定异常，模拟平台宕机。

    不用真实的死端口（如 127.0.0.1:1）：在装有 WSL2/Hyper-V 的 Windows 或
    网络沙箱里，"连不上的端口"行为不确定（可能被接受后挂起、甚至收到
    系统组件的 HTTP 错误响应），曾导致本组测试偶发失败。被测对象是
    适配器的错误映射逻辑，注入传输层异常即可完全确定。
    """

    def _fake_request(*args, **kwargs):
        raise exc

    return _fake_request


def test_platform_down_read_is_offline(monkeypatch):
    monkeypatch.setattr(
        "butler.devices.homeassistant.httpx.request",
        _transport_always_fails(httpx.ConnectError("[Errno 111] Connection refused")),
    )
    dead = HassClient(base_url="http://127.0.0.1:9", token="t", timeout_seconds=2.0)
    with pytest.raises(DeviceOffline):
        dead.get_state("light.x")


def test_platform_down_control_is_state_unknown(monkeypatch):
    monkeypatch.setattr(
        "butler.devices.homeassistant.httpx.request",
        _transport_always_fails(httpx.ConnectTimeout("timed out")),
    )
    dead = HassClient(base_url="http://127.0.0.1:9", token="t", timeout_seconds=2.0)
    with pytest.raises(StateUnknownError):
        dead.call_service("light", "turn_on", "light.x")


def test_transport_disconnect_maps_like_connect_failure(monkeypatch):
    """对端中断（如 HA 重启中的半关闭）同属传输层失败：读=离线，控制=结果不明。"""
    exc = httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    monkeypatch.setattr(
        "butler.devices.homeassistant.httpx.request", _transport_always_fails(exc)
    )
    dead = HassClient(base_url="http://127.0.0.1:9", token="t", timeout_seconds=2.0)
    with pytest.raises(DeviceOffline):
        dead.get_state("light.x")
    with pytest.raises(StateUnknownError):
        dead.call_service("light", "turn_on", "light.x")


def test_whitelist_only_registered_entities(registry):
    # 模拟器里有 sensor.living_room_humidity，但白名单没登记 → 不可访问
    with pytest.raises(DeviceNotFound):
        registry.get("sensor.living_room_humidity")


def test_build_registry_from_config_file(emulator_url, tmp_path, monkeypatch):
    config_file = tmp_path / "ha_devices.json"
    config_file.write_text(
        json.dumps(
            {
                "devices": [
                    {"entity_id": "light.living_room", "name": "客厅灯", "type": "light", "area": "客厅"}
                ]
            }
        ),
        encoding="utf-8",
    )

    class _Cfg:
        ha_url = emulator_url
        ha_token = "test-token"
        ha_timeout_seconds = 5.0
        ha_devices_file = str(config_file)

    reg = build_ha_registry(_Cfg())
    assert [d.info.id for d in reg.list()] == ["light.living_room"]


def test_build_registry_requires_whitelist_file(tmp_path):
    class _Cfg:
        ha_url = "http://127.0.0.1:1"
        ha_token = "t"
        ha_timeout_seconds = 5.0
        ha_devices_file = str(tmp_path / "missing.json")

    with pytest.raises(DeviceOffline.__bases__[0]) as excinfo:  # DeviceError
        build_ha_registry(_Cfg())
    assert "白名单" in str(excinfo.value)
