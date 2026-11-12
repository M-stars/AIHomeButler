"""阶段 14.6 HA WebSocket 测试：认证握手/命令关联/事件分发/区域映射/去抖刷新。全部离线。"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from butler.devices.discovery import DiscoveryRefresher, discover_registry
from butler.devices.ha_ws import HaWebSocket, HaWebSocketError, _ws_url

from test_discovery import FakeHassClient, _fixture_states


class FakeWSApp:
    """WebSocketApp 桩：捕获出站消息；可配置按命令类型自动回结果。"""

    def __init__(self, url, on_open=None, on_message=None, on_close=None, on_error=None):
        self.url = url
        self.on_open = on_open
        self.on_message = on_message
        self.on_close = on_close
        self.on_error = on_error
        self.sent = []
        self.closed = False
        self.auto_results = {}  # command_type -> result
        self.auto_error = {}    # command_type -> error dict

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        ctype = message.get("type", "")
        if "id" in message and ctype in self.auto_error:
            payload = {"id": message["id"], "type": "result",
                       "success": False, "error": self.auto_error[ctype]}
            self.on_message(self, json.dumps(payload))
        elif "id" in message and ctype in self.auto_results:
            payload = {"id": message["id"], "type": "result",
                       "success": True, "result": self.auto_results[ctype]}
            threading.Timer(0.02, lambda: self.on_message(self, json.dumps(payload))).start()

    def close(self):
        self.closed = True

    def run_forever(self, **kwargs):
        raise AssertionError("单测不跑真实循环")


def make_ws(auto_results=None, auto_error=None):
    cfg = SimpleNamespace(ha_url="http://localhost:8123", ha_token="secret-token")
    events = []
    ws = HaWebSocket(cfg, on_state_changed=events.append)
    # 单测不走 run 循环：直接构造桩连接并把消息回调接回 _handle_message
    app = FakeWSApp(ws._url, on_message=ws._handle_message)
    app.auto_results = auto_results or {}
    app.auto_error = auto_error or {}
    ws._app = app
    return ws, app, events


def _feed(ws, message):
    ws._handle_message(None, json.dumps(message))


def test_ws_url_conversion():
    assert _ws_url("http://localhost:8123") == "ws://localhost:8123/api/websocket"
    assert _ws_url("https://ha.example.com/") == "wss://ha.example.com/api/websocket"
    assert _ws_url("localhost:8123") == "ws://localhost:8123/api/websocket"


def test_ws_auth_handshake_and_subscription():
    ws, app, _events = make_ws()
    assert not ws.is_ready
    _feed(ws, {"type": "auth_required", "ha_version": "2026.10"})
    assert app.sent[-1] == {"type": "auth", "access_token": "secret-token"}
    _feed(ws, {"type": "auth_ok"})
    assert ws.is_ready
    assert app.sent[-1]["type"] == "subscribe_events"
    assert app.sent[-1]["event_type"] == "state_changed"


def test_ws_auth_invalid_stops_retrying():
    ws, _app, _events = make_ws()
    _feed(ws, {"type": "auth_required"})
    _feed(ws, {"type": "auth_invalid", "message": "Invalid access token"})
    assert ws._stop.is_set()  # 配置错误不自愈：停止重试避免轰炸


def test_ws_state_changed_dispatched_to_callback():
    ws, _app, events = make_ws()
    _feed(ws, {"type": "auth_ok"})
    data = {"entity_id": "light.bulb", "new_state": {"state": "on"}}
    _feed(ws, {"id": 99, "type": "event",
               "event": {"event_type": "state_changed", "data": data}})
    assert events == [data]


def test_ws_command_requires_ready():
    ws, _app, _events = make_ws()
    with pytest.raises(HaWebSocketError):
        ws.send_command("config/area_registry/list")


def test_ws_command_success_failure_and_timeout():
    ws, app, _events = make_ws(auto_results={"config/area_registry/list": [{"area_id": "a1"}]},
                               auto_error={"bad/command": {"code": "unknown"}})
    _feed(ws, {"type": "auth_ok"})
    assert ws.send_command("config/area_registry/list") == [{"area_id": "a1"}]
    with pytest.raises(HaWebSocketError):
        ws.send_command("bad/command")
    with pytest.raises(HaWebSocketError):
        ws.send_command("config/area_registry/list", timeout=0.01)  # auto_results 无此命令→超时


def test_fetch_area_map_entity_priority_and_device_fallback():
    ws, _app, _events = make_ws(auto_results={
        "config/area_registry/list": [
            {"area_id": "living", "name": "客厅"},
            {"area_id": "bed", "name": "卧室"},
        ],
        "config/entity_registry/list": [
            {"entity_id": "light.a", "area_id": "living", "device_id": "d1"},
            {"entity_id": "light.b", "device_id": "d2"},   # 实体无 area → 回落设备
            {"entity_id": "light.c"},                       # 两级都无 → 不进映射
        ],
        "config/device_registry/list": [
            {"device_id": "d2", "area_id": "bed"},
            {"device_id": "d1", "area_id": "living"},
        ],
    })
    _feed(ws, {"type": "auth_ok"})
    mapping = ws.fetch_area_map()
    assert mapping == {"light.a": "客厅", "light.b": "卧室"}


# ---------------------------------------------------------------- 刷新增强

class CountingClient(FakeHassClient):
    def __init__(self, states):
        super().__init__(states)
        self.get_states_calls = 0

    def get_states(self):
        self.get_states_calls += 1
        return super().get_states()


def test_refresher_fills_area_from_provider(tmp_path):
    client = CountingClient(_fixture_states())
    registry, _specs, _skipped = discover_registry(client)
    refresher = DiscoveryRefresher(client, registry, interval_seconds=3600,
                                   memory_store=None, exclusions_path=None)
    refresher.set_area_provider(lambda: {"light.bulb": "卧室", "fan.standing": "客厅"})
    refresher.refresh_once()
    by_id = {d.id: d for d in registry.list()}
    assert by_id["light.bulb"].info.area == "卧室"
    assert by_id["fan.standing"].info.area == "客厅"
    assert by_id["switch.socket"].info.area == ""  # 无映射留空（降级形态）


def test_refresher_area_provider_failure_degrades(tmp_path):
    client = CountingClient(_fixture_states())
    registry, _specs, _skipped = discover_registry(client)
    refresher = DiscoveryRefresher(client, registry, interval_seconds=3600)

    def boom():
        raise RuntimeError("WS 断了")

    refresher.set_area_provider(boom)
    refresher.refresh_once()  # 不抛异常，区域留空
    assert {d.info.area for d in registry.list()} == {""}


def test_refresh_now_debounces_bursts(tmp_path):
    client = CountingClient(_fixture_states())
    registry, _specs, _skipped = discover_registry(client)
    baseline = client.get_states_calls  # discover_registry 本身也拉过一次
    refresher = DiscoveryRefresher(client, registry, interval_seconds=3600,
                                   debounce_seconds=0.5)
    for _ in range(10):  # 模拟 HA 状态事件连发（全部落在去抖窗口内）
        refresher.refresh_now()
        time.sleep(0.01)
    time.sleep(0.9)  # 等去抖窗口过完
    assert client.get_states_calls - baseline == 1  # 10 次触发合并为 1 次刷新
