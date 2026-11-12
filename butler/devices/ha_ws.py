"""HA WebSocket 客户端（阶段 14.6）：状态变更订阅 + 实体/区域注册表查询。

为什么需要：REST 轮询有两慢——设备状态刷新有秒级延迟、新设备最久要等一个
发现周期；且区域归属（客厅/卧室）只在 WebSocket 注册表 API 提供，REST 没有。

设计约束：
  - websocket-client 同步实现，独立 daemon 线程收消息，契合全项目 threading 模型；
  - 断线自动重连（指数退避至 30s 封顶），重连后自动重新认证+重新订阅；
  - 令牌无效（auth_invalid）属配置错误：记 ERROR 并停止重试，不做无意义轰炸；
  - 任何失败只降级（区域为空/新设备感知退回周期刷新），绝不影响管家主流程；
  - 命令-响应按自增 id 关联（threading.Event 等待），可从任意线程调用。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

_log = logging.getLogger("butler.ha_ws")

_MAX_BACKOFF_SECONDS = 30.0
_COMMAND_TIMEOUT_SECONDS = 10.0


class HaWebSocketError(Exception):
    """HA WebSocket 不可用或命令失败（message 面向日志与降级提示）。"""


def _ws_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/api/websocket"
    return "ws://" + base + "/api/websocket"


def _default_app_factory(url, on_open, on_message, on_close, on_error):
    import websocket  # 可选依赖：未安装时降级为周期刷新

    return websocket.WebSocketApp(
        url, on_open=on_open, on_message=on_message, on_close=on_close, on_error=on_error
    )


class HaWebSocket:
    """与 HA 的单条 WebSocket 长连接：订阅 state_changed + 同步查询注册表。"""

    def __init__(
        self,
        config,
        on_state_changed: Optional[Callable[[dict], None]] = None,
        app_factory: Optional[Callable] = None,
    ) -> None:
        self._url = _ws_url(getattr(config, "ha_url", ""))
        self._token = getattr(config, "ha_token", "")
        self._on_event = on_state_changed
        self._app_factory = app_factory or _default_app_factory
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._app = None
        self._send_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, list] = {}  # id -> [Event, result_slot(list)]
        self._backoff = 1.0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="butler-ha-ws", daemon=True)
        self._thread.start()
        _log.info("HA WebSocket 已启动：%s", self._url)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def wait_ready(self, timeout: float = 8.0) -> bool:
        """等待认证+订阅就绪；超时返回 False（调用方应降级而非报错）。"""
        return self._ready.wait(timeout)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    def _run(self) -> None:
        while not self._stop.is_set():
            app = self._app_factory(
                self._url,
                on_open=lambda ws: _log.debug("WS 连接建立"),
                on_message=self._handle_message,
                on_close=lambda ws, code, msg: _log.debug("WS 连接关闭（%s %s）", code, msg),
                on_error=lambda ws, err: _log.warning("WS 连接错误：%s", err),
            )
            self._app = app
            try:
                app.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                _log.exception("WS run_forever 异常")
            self._app = None
            self._ready.clear()
            if self._stop.is_set():
                break
            _log.warning("HA WebSocket 断开，%.0fs 后重连", self._backoff)
            self._stop.wait(self._backoff)
            self._backoff = min(_MAX_BACKOFF_SECONDS, self._backoff * 2)

    # ------------------------------------------------------------ 消息处理

    def _handle_message(self, _ws, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            _log.warning("WS 收到非 JSON 消息，忽略")
            return
        mtype = message.get("type")
        if mtype == "auth_required":
            self._send_raw({"type": "auth", "access_token": self._token})
            return
        if mtype == "auth_ok":
            _log.info("HA WebSocket 认证成功，订阅 state_changed")
            self._backoff = 1.0
            self._send_raw({"id": self._new_id(), "type": "subscribe_events",
                            "event_type": "state_changed"})
            self._ready.set()
            return
        if mtype == "auth_invalid":
            # 令牌错误不会自愈：停止重试，留给用户改 .env 后重启
            _log.error("HA WebSocket 认证被拒（%s）：请检查 HA_TOKEN，停止重试",
                       message.get("message"))
            self._stop.set()
            return
        if mtype == "result":
            self._resolve(message.get("id"), message)
            return
        if mtype == "event":
            event = message.get("event") or {}
            if event.get("event_type") == "state_changed" and self._on_event is not None:
                try:
                    self._on_event(event.get("data") or {})
                except Exception:
                    _log.exception("state_changed 回调异常（不影响 WS 线程）")
            return
        _log.debug("WS 未处理消息类型：%r", mtype)

    # ------------------------------------------------------------ 命令通道

    def _new_id(self) -> int:
        with self._id_lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _send_raw(self, payload: Dict[str, Any]) -> None:
        app = self._app
        if app is None:
            raise HaWebSocketError("WebSocket 未连接")
        raw = json.dumps(payload)
        with self._send_lock:
            app.send(raw)

    def send_command(self, command_type: str, timeout: float = _COMMAND_TIMEOUT_SECONDS,
                     **payload: Any) -> Any:
        """同步执行一条 WS 命令并返回 result；失败/超时抛 HaWebSocketError。"""
        if not self._ready.is_set():
            raise HaWebSocketError("WebSocket 未就绪")
        command_id = self._new_id()
        done = threading.Event()
        slot: list = [None]
        with self._id_lock:
            self._pending[command_id] = [done, slot]
        try:
            self._send_raw({"id": command_id, "type": command_type, **payload})
        except Exception as exc:
            with self._id_lock:
                self._pending.pop(command_id, None)
            raise HaWebSocketError(f"WS 命令发送失败：{exc}") from exc
        if not done.wait(timeout):
            with self._id_lock:
                self._pending.pop(command_id, None)
            raise HaWebSocketError(f"WS 命令超时（{timeout:.0f}s）：{command_type}")
        response = slot[0] or {}
        if not response.get("success"):
            raise HaWebSocketError(
                f"WS 命令失败：{command_type}（{str(response.get('error'))[:120]}）"
            )
        return response.get("result")

    def _resolve(self, command_id: Any, message: Dict[str, Any]) -> None:
        if not isinstance(command_id, int):
            return
        with self._id_lock:
            waiter = self._pending.pop(command_id, None)
        if waiter is None:
            return
        done, slot = waiter
        slot[0] = message
        done.set()

    # ------------------------------------------------------------ 业务查询

    def fetch_area_map(self) -> Dict[str, str]:
        """查询实体→区域名称映射（实体级 area 优先，回落到所属设备）。

        任何失败抛 HaWebSocketError，由调用方决定降级策略。
        """
        areas = self.send_command("config/area_registry/list") or []
        entities = self.send_command("config/entity_registry/list") or []
        devices = self.send_command("config/device_registry/list") or []
        area_names = {
            a.get("area_id"): (a.get("name") or "") for a in areas if a.get("area_id")
        }
        device_area = {
            d.get("device_id"): d.get("area_id") for d in devices if d.get("device_id")
        }
        mapping: Dict[str, str] = {}
        for entity in entities:
            entity_id = entity.get("entity_id")
            if not entity_id:
                continue
            area_id = entity.get("area_id") or device_area.get(entity.get("device_id"))
            name = area_names.get(area_id, "")
            if name:
                mapping[entity_id] = name
        return mapping
