"""HA 适配器集成测试：在进程内启动 HA API 模拟器，走真实 HTTP 链路验证。"""
import httpx
import json
import socket
import threading
import time

import pytest
import uvicorn

from butler.devices.base import (
    DeviceError,
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
from butler.tools.home import _action_took_effect


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


@pytest.fixture
def extended_registry(client):
    from butler.devices.registry import DeviceRegistry

    devices = build_devices_from_spec(
        client,
        [
            {"entity_id": "switch.plug", "name": "客厅插座", "type": "switch", "area": "客厅"},
            {"entity_id": "cover.living_room", "name": "客厅窗帘", "type": "cover", "area": "客厅"},
            {"entity_id": "climate.living_room", "name": "客厅空调", "type": "climate", "area": "客厅"},
            {"entity_id": "humidifier.bedroom", "name": "卧室加湿器", "type": "humidifier", "area": "卧室"},
            {"entity_id": "vacuum.living_room", "name": "扫地机器人", "type": "vacuum", "area": "客厅"},
        ],
    )
    reg = DeviceRegistry()
    for device in devices:
        reg.register(device)
    return reg


def test_switch_roundtrip(extended_registry):
    plug = extended_registry.get("switch.plug")
    assert plug.read_state() == {"on": False}
    plug.apply("turn_on")
    assert plug.read_state() == {"on": True}
    plug.apply("turn_off")
    assert plug.read_state() == {"on": False}


def test_cover_roundtrip(extended_registry):
    cover = extended_registry.get("cover.living_room")
    assert cover.read_state()["open"] is False
    state = cover.apply("open")
    assert state["open"] is True and state["position"] == 100
    state = cover.apply("set_position", 40)
    assert state["position"] == 40 and state["open"] is True
    state = cover.apply("close")
    assert state["open"] is False and state["position"] == 0
    cover.apply("stop")  # 已关：stop 保持现状不报错
    assert cover.read_state()["open"] is False


def test_climate_roundtrip(extended_registry):
    ac = extended_registry.get("climate.living_room")
    state = ac.read_state()
    assert state["on"] is True and state["hvac_mode"] == "cool"
    assert state["temperature"] == 26.0 and state["current_temperature"] == 27.5
    state = ac.apply("set_temperature", 24.5)
    assert state["temperature"] == 24.5
    ac.apply("turn_off")
    state = ac.read_state()
    assert state["on"] is False and state["hvac_mode"] == "off"
    ac.apply("turn_on")
    assert ac.read_state()["on"] is True
    # 复原为初始 cool/26 开机态
    ac._client.call_service("climate", "set_hvac_mode", ac.info.id, {"hvac_mode": "cool"})
    ac.apply("set_temperature", 26.0)


def test_humidifier_roundtrip(extended_registry):
    humidifier = extended_registry.get("humidifier.bedroom")
    state = humidifier.read_state()
    assert state["on"] is True and state["humidity"] == 60 and state["current_humidity"] == 45
    state = humidifier.apply("set_humidity", 70)
    assert state["humidity"] == 70
    humidifier.apply("turn_off")
    assert humidifier.read_state()["on"] is False
    humidifier.apply("turn_on")
    humidifier.apply("set_humidity", 60)  # 复原


def test_vacuum_roundtrip(extended_registry):
    vacuum = extended_registry.get("vacuum.living_room")
    state = vacuum.read_state()
    assert state["status"] == "docked" and state["battery"] == 88
    assert vacuum.apply("start")["status"] == "cleaning"
    assert vacuum.apply("pause")["status"] == "paused"
    assert vacuum.apply("stop")["status"] == "idle"
    assert vacuum.apply("return_to_base")["status"] == "returning"
    vacuum._client.call_service("vacuum", "stop", vacuum.info.id)  # 复原为 idle（demo 语义）
    # 真实 HA 的 stop 落到 idle/docked 均在验证白名单内
    assert vacuum.read_state()["status"] in ("idle", "docked")


def test_new_devices_invalid_values_rejected(extended_registry):
    cover = extended_registry.get("cover.living_room")
    ac = extended_registry.get("climate.living_room")
    humidifier = extended_registry.get("humidifier.bedroom")
    with pytest.raises(DeviceError):
        cover.apply("set_position", 101)
    with pytest.raises(DeviceError):
        cover.apply("set_position", "half")
    with pytest.raises(DeviceError):
        ac.apply("set_temperature", "warm")
    with pytest.raises(DeviceError):
        ac.apply("set_temperature", 200)
    with pytest.raises(DeviceError):
        humidifier.apply("set_humidity", -1)


def test_cover_climate_verification_semantics():
    """工具层回读验证：窗帘进行中算成功、扫地机器人状态集、温度容差比较。"""
    assert _action_took_effect("open", None, {"open": True, "position": 100, "activity": "open"})
    assert _action_took_effect("open", None, {"open": False, "position": 30, "activity": "opening"})
    assert not _action_took_effect("open", None, {"open": False, "position": 0, "activity": "closed"})
    assert _action_took_effect("close", None, {"open": False, "position": 0, "activity": "closing"})
    assert _action_took_effect("set_position", 40, {"open": True, "position": 40, "activity": "open"})
    assert not _action_took_effect("set_position", 40, {"open": True, "position": 55, "activity": "open"})
    assert _action_took_effect("start", None, {"status": "cleaning", "battery": 80})
    assert _action_took_effect("stop", None, {"status": "idle", "battery": 80})
    assert _action_took_effect("stop", None, {"status": "docked", "battery": 80})
    assert _action_took_effect("return_to_base", None, {"status": "returning", "battery": 70})
    assert _action_took_effect("set_temperature", 26.5, {"on": True, "temperature": 26.5})
    assert not _action_took_effect("set_temperature", 26.5, {"on": True, "temperature": None})
    assert _action_took_effect("set_humidity", 70, {"on": True, "humidity": 70})


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
