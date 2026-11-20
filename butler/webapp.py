"""Web 界面与 API（阶段 9）：FastAPI 单页仪表盘 + 对话 + 确认队列 + 记忆管理。

安全设计：
  - 与 CLI 共用同一 Agent/工具/安全层；控制类操作的确认通过"确认队列"完成：
    对话请求阻塞等待用户在页面上批准/拒绝（超时按拒绝处理），绝不静默放行；
  - 同一时刻只允许一个对话/任务在执行（防止并发操作同一设备与上下文错乱）；
  - API 全部要求访问令牌（评审 4.3）：.env 设 WEB_TOKEN 即用固定值，
    留空则每次启动自动生成随机令牌并打印到控制台；令牌错误一律 401，
    防止本机其他进程或浏览器里访问的恶意网页（CSRF）操控设备/读隐私。

阶段 12 扩展：音色库管理 API（/api/voices*）与语音合成 API（/api/voice*）。
语音栈为可选装配（voice_stack=None 时这些端点返回 503），不影响原有功能。

阶段 14 扩展：审计日志（/api/audit，评审 4.4）与每日 token 预算展示（评审 4.5）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import secrets
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .devices.base import ControllableDevice, DeviceError, DeviceNotFound, StateUnknownError
from .devices.registry import DeviceRegistry
from .memory.store import MemoryStore
from .planner import PlanRunner
from .proactive import RuleEngine
from .tools.executor import ToolExecutor
from .voice.cosyvoice_client import CosyVoiceClientError
from .voice.profiles import VoiceProfileError
from .voice.speakers import SpeakerError
from .voice.tts import VoiceError

_WEB_DIR = Path(__file__).parent / "web"
_log = logging.getLogger("butler.webapp")

_PERSONA_PRESETS: List[Dict[str, str]] = [
    {
        "id": "default",
        "name": "默认管家",
        "prompt": "",
        "description": "沉稳、简洁、自然的家庭 AI 管家默认风格。",
    },
    {
        "id": "concise",
        "name": "极简高效",
        "prompt": "回复尽量控制在 1-2 句话内，先给结果和关键数据，省略寒暄和铺垫。",
        "description": "适合高频指令控制，直奔主题。",
    },
    {
        "id": "elder",
        "name": "长辈关怀",
        "prompt": "语气亲切温和、通俗易懂，避免英文缩写和专业术语，汇报温度和设备状态时顺带给出贴心提醒。",
        "description": "适合家中长辈使用，清晰易懂。",
    },
    {
        "id": "warm",
        "name": "温暖陪伴",
        "prompt": "语气温柔体贴、有生活气息，在严谨执行设备指令的同时给予轻松自然的日常回应。",
        "description": "更有温度的家庭日常交互体验。",
    },
]


class EventHub:
    """线程安全的内存 SSE 事件广播器（用于实时推送安全流水线/确认/设备变更事件）。"""

    def __init__(self, max_queue_size: int = 128) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[queue.Queue] = []
        self._max_queue_size = max_queue_size

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._max_queue_size)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        payload = {
            "event": event_type,
            "data": data,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass


class PairingManager:
    """局域网配对码管理器（6 位数字、限时 10 分钟、最多 3 次尝试、一次性消费）。"""

    def __init__(self, ttl_seconds: int = 600, max_attempts: int = 3) -> None:
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._lock = threading.Lock()
        self._code: Optional[str] = None
        self._expires_monotonic: float = 0.0
        self._expires_at_iso: str = ""
        self._attempts_left: int = 0

    def start(self) -> Dict[str, Any]:
        with self._lock:
            self._code = f"{secrets.randbelow(1_000_000):06d}"
            self._expires_monotonic = time.monotonic() + self._ttl
            self._expires_at_iso = (
                datetime.now().astimezone() + timedelta(seconds=self._ttl)
            ).isoformat(timespec="seconds")
            self._attempts_left = self._max_attempts
            return {
                "code": self._code,
                "expires_in_seconds": self._ttl,
                "expires_at": self._expires_at_iso,
                "max_attempts": self._max_attempts,
            }

    def verify_and_consume(self, code: str) -> tuple:
        """返回 (ok: bool, error_message: str)。"""
        supplied = (code or "").strip()
        with self._lock:
            if not self._code:
                return False, "当前没有有效的配对码，请先在管家端生成配对码"
            if time.monotonic() > self._expires_monotonic:
                self._code = None
                return False, "配对码已过期，请重新生成"
            if not supplied or not secrets.compare_digest(supplied, self._code):
                self._attempts_left -= 1
                if self._attempts_left <= 0:
                    self._code = None
                    return False, "配对码错误次数过多，已自动作废，请重新生成"
                return False, f"配对码错误（剩余 {self._attempts_left} 次机会）"
            self._code = None
            return True, ""


class WebConfirmer:
    """Web 端确认器：把确认请求放入队列，等待页面批准/拒绝；超时按拒绝处理。"""

    def __init__(
        self,
        timeout_seconds: float = 120.0,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._timeout = float(timeout_seconds)
        self._lock = threading.Lock()
        self._pending: List[Dict[str, Any]] = []
        self._on_event = on_event

    def set_event_callback(
        self, on_event: Optional[Callable[[str, Dict[str, Any]], None]]
    ) -> None:
        self._on_event = on_event

    def confirm(self, summary: str, arguments: Dict[str, Any]) -> bool:
        item = {
            "id": uuid.uuid4().hex[:12],
            "summary": summary,
            "arguments": arguments,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": threading.Event(),
            "approved": False,
        }
        with self._lock:
            self._pending.append(item)
        if self._on_event is not None:
            self._on_event(
                "confirmation_pending",
                {k: v for k, v in item.items() if k != "event"},
            )
        resolved = item["event"].wait(self._timeout)
        with self._lock:
            if item in self._pending:
                self._pending.remove(item)
        if not resolved and not item["event"].is_set():
            # 超时且确实无人应答才按拒绝处理；若应答恰好落在超时临界点
            # （wait 已返回但 respond 已写入结果），以写入结果为准（P2 竞态修复）
            item["approved"] = False
        if self._on_event is not None:
            self._on_event(
                "confirmation_resolved",
                {"id": item["id"], "approved": bool(item["approved"]), "summary": summary},
            )
        return bool(item["approved"])

    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in item.items() if k not in ("event",)}
                for item in self._pending
            ]

    def respond(self, confirmation_id: str, approved: bool) -> bool:
        with self._lock:
            for item in self._pending:
                if item["id"] == confirmation_id:
                    item["approved"] = bool(approved)
                    item["event"].set()
                    return True
        return False


class ChatRequest(BaseModel):
    text: str


class TaskRequest(BaseModel):
    text: str


class ConfirmationRequest(BaseModel):
    approved: bool


class MemoryCreateRequest(BaseModel):
    kind: str
    content: str


class VoiceTestRequest(BaseModel):
    text: str


class TTSRequest(BaseModel):
    text: str


class ConsentRequest(BaseModel):
    granted: bool


class DeviceCommandRequest(BaseModel):
    action: str
    value: Optional[Any] = None


class PairConfirmRequest(BaseModel):
    code: str
    device_name: str
    platform: str = "unknown"


class PersonaUpdateRequest(BaseModel):
    persona: str = ""


def _voice_http_error(exc: Exception) -> HTTPException:
    """把语音层异常映射为 HTTP 状态码与用户可读信息。"""
    if isinstance(exc, (VoiceProfileError, SpeakerError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, CosyVoiceClientError):
        status = {
            "timeout": 504,
            "unavailable": 503,
            "busy": 429,
            "bad_request": 400,
        }.get(exc.kind, 502)
        return HTTPException(status_code=status, detail=str(exc))
    if isinstance(exc, VoiceError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=f"语音服务内部错误：{exc}")


def create_app(
    *,
    config,
    registry: DeviceRegistry,
    memory_store: MemoryStore,
    agent,
    rule_engine: RuleEngine,
    confirmer: WebConfirmer,
    planner: Optional[PlanRunner] = None,
    voice_stack=None,
    speaker_identity=None,
    auth_token: Optional[str] = None,
    token_budget=None,
    household_store=None,
    executor: Optional[ToolExecutor] = None,
    ha_client=None,
) -> FastAPI:
    app = FastAPI(title="AI Home Butler", docs_url=None, redoc_url=None)
    # 访问令牌：未显式传入（或配置为空）时自动生成随机值，启动时打印到控制台
    app.state.auth_token = auth_token or secrets.token_urlsafe(24)
    started_at = time.monotonic()

    event_hub = EventHub()
    pairing_mgr = PairingManager()
    app.state.event_hub = event_hub
    app.state.pairing_mgr = pairing_mgr

    # 挂载确认器与执行器实时事件（供 SSE 安全流水线面板与 App/小程序推送）
    if hasattr(confirmer, "set_event_callback"):
        confirmer.set_event_callback(event_hub.publish)
    if executor is not None and hasattr(executor, "set_event_listener"):
        executor.set_event_listener(lambda ev: event_hub.publish("pipeline_step", ev))

    # CORS：为 App（Capacitor/WebView）/ 小程序 / 本地开发调试预留跨源支持
    # 不启用 allow_credentials（不用 Cookie，全部显式令牌头，杜绝 CSRF）
    raw_cors = (getattr(config, "web_cors_origins", "") or "").strip()
    if raw_cors:
        cors_origins = ["*"] if raw_cors == "*" else [
            o.strip() for o in raw_cors.split(",") if o.strip()
        ]
    else:
        cors_origins = ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Butler-Token"],
    )

    _PUBLIC_PATHS = {"/", "/manifest.json", "/api/health", "/api/pair/confirm"}

    # ---- 多令牌访问鉴权（主令牌 + 已配对 App/小程序设备令牌）----
    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or path in _PUBLIC_PATHS
            or path.startswith("/static/")
        ):
            return await call_next(request)

        supplied = (
            request.headers.get("X-Butler-Token")
            or request.query_params.get("token")
        )
        if not supplied:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                supplied = auth_header[len("Bearer "):]

        if supplied and secrets.compare_digest(supplied, app.state.auth_token):
            request.state.client_actor = "user"
            request.state.is_primary_token = True
            return await call_next(request)

        paired = (
            memory_store.verify_pairing_token(supplied)
            if supplied and hasattr(memory_store, "verify_pairing_token")
            else None
        )
        if paired is not None:
            request.state.client_actor = f"device_app:{paired['device_id']}"
            request.state.is_primary_token = False
            request.state.paired_device = paired
            return await call_next(request)

        return Response(
            content='{"detail":"未授权：缺少或错误的访问令牌"}',
            status_code=401,
            media_type="application/json",
        )

    static_dir = _WEB_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    state: Dict[str, Any] = {
        "agent_lock": threading.Lock(),
        "task_lock": threading.Lock(),
        "tts_lock": threading.Lock(),
    }

    def _device_views() -> List[Dict[str, Any]]:
        views = []
        for device in registry.list():
            # actions：动态能力清单（阶段 14.5 自动发现推导），应用设备面板按它渲染控件
            supported = getattr(device, "supported_actions", None)
            item = {
                "id": device.info.id,
                "name": device.info.name,
                "type": device.info.device_type,
                "area": device.info.area,
                "available": device.is_available(),
                "actions": list(supported()) if callable(supported) else [],
                "state": None,
            }
            try:
                item["state"] = device.read_state()
            except DeviceError:
                pass
            views.append(item)
        return views

    @app.get("/")
    def index():
        return FileResponse(_WEB_DIR / "index.html")

    @app.get("/manifest.json")
    def pwa_manifest():
        manifest_path = _WEB_DIR / "manifest.json"
        if not manifest_path.exists():
            raise HTTPException(status_code=404, detail="manifest not found")
        return FileResponse(manifest_path, media_type="application/manifest+json")

    @app.get("/api/health")
    def health():
        """公开轻量健康探针（供 App / 小程序局域网与云通道自动切换探测）。"""
        return {
            "status": "ok",
            "service": "ai-home-butler",
            "version": "2.0",
            "protocol_version": "1.0",
            "uptime_seconds": int(time.monotonic() - started_at),
        }

    def _ha_breaker_status() -> Optional[Dict[str, Any]]:
        if ha_client is not None and hasattr(ha_client, "circuit_breaker_status"):
            return ha_client.circuit_breaker_status()
        return None

    @app.get("/api/system/info")
    def system_info():
        """客户端能力发现接口（供 Web / App / 小程序动态适配功能模块）。"""
        pairings = (
            memory_store.list_pairings()
            if hasattr(memory_store, "list_pairings")
            else []
        )
        return {
            "version": "2.0",
            "protocol_version": "1.0",
            "model": config.model,
            "backend": config.device_backend,
            "confirmation_mode": config.confirmation_mode,
            "tts_backend": getattr(config, "tts_backend", "sapi"),
            "uptime_seconds": int(time.monotonic() - started_at),
            "ha_circuit_breaker": _ha_breaker_status(),
            "paired_devices_count": len(pairings),
            "capabilities": {
                "direct_control": True,
                "sse_events": True,
                "pairing": True,
                "routines": True,
                "planner": planner is not None,
                "voice": voice_stack is not None,
                "speakers": speaker_identity is not None,
                "household": household_store is not None,
            },
        }

    @app.get("/api/events")
    async def sse_events(request: Request):
        """SSE 实时事件流：推送安全流水线步骤、确认请求、设备状态变更与主动提醒。"""
        q = event_hub.subscribe()

        async def _stream():
            try:
                hello = json.dumps(
                    {"status": "connected", "ts": datetime.now().astimezone().isoformat(timespec="seconds")},
                    ensure_ascii=False,
                )
                yield f"event: connected\ndata: {hello}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.to_thread(q.get, True, 15.0)
                        data_str = json.dumps(item["data"], ensure_ascii=False)
                        yield f"event: {item['event']}\ndata: {data_str}\n\n"
                    except queue.Empty:
                        yield ": heartbeat\n\n"
            finally:
                event_hub.unsubscribe(q)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/overview")
    def overview():
        return {
            "devices": _device_views(),
            "model": config.model,
            "backend": config.device_backend,
            "confirmation_mode": config.confirmation_mode,
            # 旧配置桩可能没有该字段：语音后端仅作展示用，缺省按 sapi 呈现
            "tts_backend": getattr(config, "tts_backend", "sapi"),
            "token_budget": token_budget.status() if token_budget is not None else None,
            "ha_circuit_breaker": _ha_breaker_status(),
        }

    # ---- 设备直控 API（APP_DESIGN.md §4：不经 AI，跳过确认但保留校验/防重放/回读/审计）----

    @app.get("/api/devices")
    def list_devices():
        return {"devices": _device_views()}

    @app.get("/api/devices/{device_id}/state")
    def get_single_device_state(device_id: str):
        try:
            device = registry.get(device_id)
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        supported = getattr(device, "supported_actions", None)
        state_val = None
        available = device.is_available()
        try:
            state_val = device.read_state()
        except DeviceError:
            available = False
        return {
            "id": device.info.id,
            "name": device.info.name,
            "type": device.info.device_type,
            "area": device.info.area,
            "available": available,
            "actions": list(supported()) if callable(supported) else [],
            "state": state_val,
        }

    @app.post("/api/devices/{device_id}/command")
    def direct_device_command(
        device_id: str, payload: DeviceCommandRequest, request: Request
    ):
        """Web / App / 小程序设备直控接口：复用安全执行流水线，仅豁免交互确认。"""
        try:
            device = registry.get(device_id)
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not isinstance(device, ControllableDevice):
            raise HTTPException(status_code=403, detail=f"设备 {device.info.name} 不可控制")
        supported = device.supported_actions()
        if payload.action not in supported:
            raise HTTPException(
                status_code=400,
                detail=f"设备 {device.info.name} 不支持动作 {payload.action!r}，支持：{', '.join(supported)}",
            )

        actor = getattr(request.state, "client_actor", "web_direct")
        if actor == "user":
            actor = "web_direct"

        args: Dict[str, Any] = {"device_id": device_id, "action": payload.action}
        if payload.value is not None:
            args["value"] = payload.value

        if executor is not None:
            res = executor.execute_direct("control_device", args, actor=actor)
            if res.status == "error":
                raise HTTPException(status_code=400, detail=res.message)
            event_hub.publish(
                "device_updated",
                {
                    "device_id": device_id,
                    "action": payload.action,
                    "status": res.status,
                    "stage": res.stage,
                    "state": res.data.get("state_after"),
                },
            )
            return {
                "device_id": device_id,
                "action": payload.action,
                "status": res.status,
                "stage": res.stage,
                "message": res.message,
                "verified": res.data.get("verified", False),
                "state_after": res.data.get("state_after"),
            }

        # 兼容未注入 executor 的旧测试桩
        try:
            device.apply(payload.action, payload.value)
            state_after = device.read_state()
        except StateUnknownError as exc:
            memory_store.add_audit(
                "device.direct", f"{device_id} {payload.action}", "unknown", actor=actor
            )
            return {
                "device_id": device_id,
                "action": payload.action,
                "status": "unknown",
                "stage": "unknown",
                "message": str(exc),
                "verified": False,
                "state_after": None,
            }
        except DeviceError as exc:
            memory_store.add_audit(
                "device.direct", f"{device_id} {payload.action}", "error", actor=actor
            )
            raise HTTPException(status_code=400, detail=str(exc))

        memory_store.add_audit(
            "device.direct", f"{device_id} {payload.action}", "ok", actor=actor
        )
        event_hub.publish(
            "device_updated",
            {"device_id": device_id, "action": payload.action, "status": "ok", "state": state_after},
        )
        return {
            "device_id": device_id,
            "action": payload.action,
            "status": "ok",
            "stage": "verified",
            "message": "执行成功",
            "verified": True,
            "state_after": state_after,
        }

    # ---- 客户端配对协议（APP_DESIGN.md §2：为 App / 小程序预留多令牌配对）----

    @app.post("/api/pair/start")
    def pair_start(request: Request):
        if not getattr(request.state, "is_primary_token", True):
            raise HTTPException(status_code=403, detail="仅主管理令牌可发起新设备配对")
        info = pairing_mgr.start()
        memory_store.add_audit("pairing.start", "生成 6 位局域网配对码", "ok", actor="user")
        return info

    @app.post("/api/pair/confirm")
    def pair_confirm(payload: PairConfirmRequest):
        device_name = (payload.device_name or "").strip()
        if not device_name:
            raise HTTPException(status_code=400, detail="device_name 不能为空")
        ok, err = pairing_mgr.verify_and_consume(payload.code)
        if not ok:
            memory_store.add_audit("pairing.confirm", f"配对失败（{device_name}）：{err}", "error", actor="system")
            raise HTTPException(status_code=400, detail=err)
        device_id = f"cli_{uuid.uuid4().hex[:10]}"
        raw_token = f"bh_{secrets.token_urlsafe(32)}"
        record = memory_store.add_pairing(
            device_id=device_id,
            device_name=device_name,
            raw_token=raw_token,
            platform=payload.platform or "unknown",
        )
        return {
            "device_id": record["device_id"],
            "device_name": record["device_name"],
            "platform": record["platform"],
            "token": raw_token,
            "protocol_version": "1.0",
        }

    @app.get("/api/pair/devices")
    def pair_list():
        return {"devices": memory_store.list_pairings()}

    @app.delete("/api/pair/devices/{device_id}")
    def pair_revoke(device_id: str, request: Request):
        if not getattr(request.state, "is_primary_token", True):
            raise HTTPException(status_code=403, detail="仅主管理令牌可踢除已配对设备")
        if not memory_store.revoke_pairing(device_id):
            raise HTTPException(status_code=404, detail="已配对设备不存在或已失效")
        return {"device_id": device_id, "revoked": True}

    # ---- 设置与人设 API（APP_DESIGN.md §5.3 / §5.5）----

    @app.get("/api/settings/persona")
    def get_persona():
        current = memory_store.get_setting("persona", "") if hasattr(memory_store, "get_setting") else ""
        return {"persona": current or "", "presets": _PERSONA_PRESETS}

    @app.put("/api/settings/persona")
    def update_persona(payload: PersonaUpdateRequest, request: Request):
        text = (payload.persona or "").strip()
        if len(text) > 1000:
            raise HTTPException(status_code=400, detail="人设提示词长度不能超过 1000 字")
        memory_store.set_setting("persona", text)
        actor = getattr(request.state, "client_actor", "user")
        memory_store.add_audit(
            "settings.persona",
            f"更新管家个性化人设（{len(text)} 字）" if text else "恢复默认人设",
            "ok",
            actor=actor,
        )
        return {"persona": text, "saved": True}

    @app.get("/api/settings/models")
    def get_models_settings():
        """返回脱敏后的模型配置视图（绝不回传 API Key 明文）。"""
        profiles = []
        for name in ("main", "planner"):
            if hasattr(config, "model_profile"):
                try:
                    p = config.model_profile(name)
                    key_masked = (
                        f"{p.api_key[:4]}****{p.api_key[-4:]}"
                        if len(p.api_key) >= 10
                        else ("****" if p.api_key else "")
                    )
                    profiles.append({
                        "name": p.name,
                        "base_url": p.base_url,
                        "model": p.model,
                        "api_key_masked": key_masked,
                        "timeout_seconds": p.timeout_seconds,
                    })
                except Exception:
                    pass
        if not profiles:
            profiles.append({
                "name": "main",
                "base_url": getattr(config, "base_url", ""),
                "model": getattr(config, "model", ""),
                "api_key_masked": "****",
                "timeout_seconds": getattr(config, "llm_timeout_seconds", 60.0),
            })
        return {"profiles": profiles}

    @app.get("/api/rules")
    def list_rules():
        return {"rules": memory_store.list_rules()}

    @app.post("/api/chat")
    def chat(payload: ChatRequest):
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="内容不能为空")
        if not state["agent_lock"].acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有对话在处理中，请稍候")
        try:
            result = agent.process(text)
        finally:
            state["agent_lock"].release()
        try:
            fired = rule_engine.run_checks()
        except Exception:
            fired = []
        return {
            "reply": result.reply,
            "tool_events": [
                {"tool": e.tool, "status": e.status, "message": e.message}
                for e in result.tool_events
            ],
            "notifications": fired,
        }

    @app.post("/api/task")
    def run_task(payload: TaskRequest):
        if planner is None:
            raise HTTPException(status_code=503, detail="任务规划器未装配")
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="内容不能为空")
        if not state["task_lock"].acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有任务在执行中，请稍候")
        try:
            return planner.run(text)
        finally:
            state["task_lock"].release()

    @app.get("/api/confirmations")
    def pending_confirmations():
        return {"confirmations": confirmer.pending()}

    @app.post("/api/confirmations/{confirmation_id}")
    def respond_confirmation(confirmation_id: str, payload: ConfirmationRequest):
        # 先取摘要再应答：respond 后确认项会被等待线程移除
        summary = next(
            (c["summary"] for c in confirmer.pending() if c["id"] == confirmation_id),
            confirmation_id,
        )
        ok = confirmer.respond(confirmation_id, payload.approved)
        if not ok:
            raise HTTPException(status_code=404, detail="确认请求不存在或已处理")
        memory_store.add_audit(
            "confirmation.respond",
            f"{'批准' if payload.approved else '拒绝'}：{summary}",
            "ok" if payload.approved else "declined",
            actor="user",
        )
        return {"resolved": True, "approved": payload.approved}

    @app.get("/api/memories")
    def memories():
        return {"memories": memory_store.list()}

    @app.post("/api/memories")
    def add_memory(payload: MemoryCreateRequest):
        try:
            memory_id = memory_store.add(payload.kind, payload.content, source="user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"id": memory_id, "saved": True}

    @app.delete("/api/memories/{memory_id}")
    def delete_memory(memory_id: int):
        if not memory_store.delete(memory_id):
            raise HTTPException(status_code=404, detail="记忆不存在")
        return {"deleted": True}

    @app.get("/api/notifications")
    def notifications():
        # 通知由后台 RuleScheduler 落库；此处只读，
        # 避免前端 8 秒轮询反复触发规则检查导致连击计数虚高
        return {"notifications": memory_store.list_notifications()}

    @app.get("/api/tasks")
    def tasks():
        return {"tasks": memory_store.list_task_logs()}

    @app.get("/api/audit")
    def audit(limit: int = 100):
        return {"audit": memory_store.list_audit(limit=limit)}

    # ---- 家庭生活服务只读视图（阶段 14.7；写入口走对话工具，带确认与审计）----

    @app.get("/api/pantry")
    def pantry_view():
        if household_store is None:
            raise HTTPException(status_code=503, detail="家庭生活服务未装配")
        from datetime import date

        from .household import days_left

        today = date.today()
        items = []
        for item in household_store.list_food(status="in_stock"):
            left = days_left(item, today)
            items.append({
                "id": item["id"], "name": item["name"], "category": item["category"],
                "quantity": item["quantity"], "added_date": item["added_date"],
                "expires_in_days": item["expires_in_days"], "days_left": left,
                "expired": left is not None and left < 0,
            })
        items.sort(key=lambda x: (x["days_left"] is None, x["days_left"] or 0))
        return {"items": items, "count": len(items)}

    @app.get("/api/wardrobe")
    def wardrobe_view():
        if household_store is None:
            raise HTTPException(status_code=503, detail="家庭生活服务未装配")
        from .household import wash_policy

        garments = []
        for g in household_store.list_garments():
            policy = wash_policy(g)
            garments.append({
                "id": g["id"], "owner": g["owner"], "name": g["name"],
                "category": g["category"], "warmth": g["warmth"], "status": g["status"],
                "times_worn_since_wash": g["times_worn_since_wash"],
                "last_wash_date": g["last_wash_date"],
                "wash_interval_days": policy["interval"] if policy else None,
            })
        return {"garments": garments, "count": len(garments)}

    # ---- 例行任务：预授权的定时设备动作（创建即授权，执行留审计）----

    @app.get("/api/routines")
    def routines_list():
        return {"routines": memory_store.list_routines()}

    @app.post("/api/routines")
    def routines_create(body: dict):
        from .proactive import normalize_days, _parse_hhmm
        from .routines import _MAX_ACTIONS, validate_step

        name = body.get("name")
        actions = body.get("actions")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="name 不能为空")
        if _parse_hhmm(body.get("time")) is None:
            raise HTTPException(status_code=400, detail="time 需要是 24 小时制 HH:MM（如 07:00）")
        if not isinstance(actions, list) or not actions:
            raise HTTPException(status_code=400, detail="actions 需要至少一个动作")
        if len(actions) > _MAX_ACTIONS:
            raise HTTPException(status_code=400, detail=f"单个例行任务最多 {_MAX_ACTIONS} 个动作")
        try:
            days = normalize_days(body.get("days", "daily"))
            validated = [validate_step(registry, step) for step in actions]
        except (ValueError, DeviceError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        schedule: Dict[str, Any] = {"time": body["time"].strip(), "days": days}
        if body.get("catchup_minutes") is not None:
            catchup = body["catchup_minutes"]
            if isinstance(catchup, bool) or not isinstance(catchup, int) or not 0 <= catchup <= 720:
                raise HTTPException(status_code=400, detail="catchup_minutes 需要是 0-720 的整数")
            schedule["catchup_minutes"] = catchup
        routine_id = memory_store.add_routine(
            name=name.strip(),
            schedule=schedule,
            actions=actions,
            notify_on_success=bool(body.get("notify_on_success", False)),
        )
        memory_store.add_audit(
            action="routine_create",
            detail=f"#{routine_id} {name.strip()}（{len(actions)} 步，已逐动作预检）",
            status="ok",
        )
        return {"routine_id": routine_id, "created": True, "steps": validated}

    @app.delete("/api/routines/{routine_id}")
    def routines_delete(routine_id: int):
        if not memory_store.delete_routine(routine_id):
            raise HTTPException(status_code=404, detail="例行任务不存在")
        memory_store.add_audit(action="routine_delete", detail=f"#{routine_id}", status="ok")
        return {"routine_id": routine_id, "deleted": True}

    @app.post("/api/routines/{routine_id}/toggle")
    def routines_toggle(routine_id: int):
        current = next(
            (r for r in memory_store.list_routines() if r["id"] == routine_id), None
        )
        if current is None:
            raise HTTPException(status_code=404, detail="例行任务不存在")
        enabled = not current["enabled"]
        memory_store.set_routine_enabled(routine_id, enabled)
        return {"routine_id": routine_id, "enabled": enabled}

    # ---- 语音：状态 / 音色库 / 合成（阶段 12，voice_stack 未装配时全部 503）----

    def _require_voice_stack():
        if voice_stack is None:
            raise HTTPException(status_code=503, detail="语音模块未装配")
        return voice_stack

    @app.get("/api/voice/status")
    def voice_status():
        stack = _require_voice_stack()
        try:
            return stack.status()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"语音状态获取失败：{exc}")

    @app.get("/api/voices")
    def list_voices():
        stack = _require_voice_stack()
        voices = stack.profiles.list()
        # 对外不暴露本地绝对路径：只返回受管相对路径文件名
        for v in voices:
            v.pop("_audio_file", None)
        return {"voices": voices, "active_voice_id": stack.cosyvoice_speaker.voice_id}

    @app.post("/api/voices")
    async def create_voice(
        display_name: str = Form(),
        reference_text: str = Form(),
        consent_granted: bool = Form(default=False),
        prompt_wav: UploadFile = File(),
    ):
        stack = _require_voice_stack()
        try:
            data = await prompt_wav.read()
            profile = stack.profiles.create(
                display_name,
                reference_text,
                data,
                Path(prompt_wav.filename or "").suffix,
                consent_granted=consent_granted,
            )
        except VoiceProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        memory_store.add_audit(
            "voice.create", f"音色「{profile['display_name']}」（{profile['voice_id']}）", "ok", actor="user"
        )
        return {"voice": profile, "created": True}

    @app.post("/api/voices/preset")
    def create_preset_voice():
        """把官方示例音频登记为预设音色（consent 视为已授予：官方公开示例）。"""
        stack = _require_voice_stack()
        asset_dir = Path(os.environ.get(
            "COSYVOICE_ASSET_DIR",
            Path(__file__).resolve().parent.parent / "cosyvoice" / "CosyVoice" / "asset",
        ))
        audio = asset_dir / "zero_shot_prompt.wav"
        try:
            profile = stack.profiles.create(
                "官方示例音色",
                "希望你以后能够做的比我还好呦。",
                b"",
                ".wav",
                model_id="preset",
                consent_granted=True,
                source_path=audio,
            )
        except VoiceProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        memory_store.add_audit(
            "voice.create", f"预设音色「{profile['display_name']}」（{profile['voice_id']}）", "ok", actor="user"
        )
        return {"voice": profile, "created": True}

    @app.post("/api/voices/{voice_id}/default")
    def set_default_voice(voice_id: str):
        stack = _require_voice_stack()
        try:
            profile = stack.profiles.set_default(voice_id)
            stack.cosyvoice_speaker.set_voice(voice_id)  # 立即生效到合成层
        except VoiceProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"voice": profile, "is_default": True}

    @app.post("/api/voices/{voice_id}/consent")
    def set_voice_consent(voice_id: str, payload: ConsentRequest):
        stack = _require_voice_stack()
        try:
            profile = stack.profiles.update_consent(voice_id, payload.granted)
        except VoiceProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"voice": profile}

    @app.delete("/api/voices/{voice_id}")
    def delete_voice(voice_id: str):
        stack = _require_voice_stack()
        try:
            removed = stack.profiles.delete(voice_id)
        except VoiceProfileError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if stack.cosyvoice_speaker.voice_id == voice_id:
            stack.cosyvoice_speaker.set_voice(None)  # 回退到模型预设音色
        # 删除音色时一并清除其生成缓存（隐私默认：档案删除则生成物随之清除）
        stack.cache.purge_voice(voice_id)
        memory_store.add_audit(
            "voice.delete", f"音色「{removed['display_name']}」（{voice_id}）及其生成缓存", "ok", actor="user"
        )
        return {"deleted": True, "voice": removed}

    @app.post("/api/voices/{voice_id}/test")
    def test_voice(voice_id: str, payload: VoiceTestRequest):
        """用指定音色试听一段文本，返回 WAV 音频（audio/wav）。"""
        stack = _require_voice_stack()
        try:
            profile = stack.profiles.resolve_for_synthesis(voice_id)
            wav = stack.client.synthesize(payload.text, profile)
        except (VoiceProfileError, CosyVoiceClientError) as exc:
            raise _voice_http_error(exc)
        return Response(content=wav, media_type="audio/wav")

    @app.post("/api/tts")
    def tts(payload: TTSRequest):
        """按当前配置的后端把文本合成为 WAV（用于朗读管家回复/自定义文本）。"""
        stack = _require_voice_stack()
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="内容不能为空")
        try:
            with state["tts_lock"]:
                if stack.backend == "cosyvoice":
                    wav = stack.cosyvoice_speaker.synthesize_bytes(text)
                else:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                        tmp = Path(tf.name)
                    try:
                        stack.speaker.speak_to_file(text, tmp)
                        wav = tmp.read_bytes()
                    finally:
                        tmp.unlink(missing_ok=True)
        except (VoiceProfileError, CosyVoiceClientError, VoiceError) as exc:
            raise _voice_http_error(exc)
        return Response(content=wav, media_type="audio/wav")

    # ---- 家庭成员声纹（阶段 13；未装配时 503，向量永不出服务端/数据目录）----

    def _require_speaker_identity():
        if speaker_identity is None:
            raise HTTPException(status_code=503, detail="声纹模块未装配")
        return speaker_identity

    @app.get("/api/speakers")
    def speakers_status():
        identity = _require_speaker_identity()
        body = identity.status()
        body["service_healthy"] = (
            voice_stack.client.health_check(timeout_seconds=2.0)
            if voice_stack is not None and getattr(voice_stack, "client", None) is not None
            else False
        )
        return body

    @app.post("/api/speakers")
    async def enroll_speaker(
        display_name: str = Form(),
        consent_granted: bool = Form(),
        voice_id: str = Form(default=""),
        wav: UploadFile = File(),
    ):
        identity = _require_speaker_identity()
        if voice_stack is None or getattr(voice_stack, "client", None) is None:
            raise HTTPException(status_code=503, detail="语音声纹服务未装配")
        try:
            data = await wav.read()
            # 原始录音只在内存中过一遍：提取向量后即丢弃，不做落盘留存
            embedding = voice_stack.client.speaker_embedding(
                data, filename=Path(wav.filename or "clip.wav").name
            )
            profile = identity.registry.enroll(
                display_name,
                embedding,
                consent_granted=consent_granted,
                voice_id=voice_id or None,
            )
        except (SpeakerError, CosyVoiceClientError) as exc:
            raise _voice_http_error(exc)
        memory_store.add_audit(
            "speaker.enroll",
            f"成员「{profile['display_name']}」（{profile['member_id']}）声纹注册，授权={consent_granted}",
            "ok", actor="user",
        )
        return {"speaker": profile, "created": True}

    @app.post("/api/speakers/{member_id}/voice")
    def bind_speaker_voice(member_id: str, voice_id: str = Form(default="")):
        identity = _require_speaker_identity()
        try:
            profile = identity.registry.update_voice(member_id, voice_id or None)
        except SpeakerError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"speaker": profile}

    @app.post("/api/speakers/{member_id}/name")
    def rename_speaker(member_id: str, display_name: str = Form()):
        identity = _require_speaker_identity()
        try:
            profile = identity.registry.update_name(member_id, display_name)
        except SpeakerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"speaker": profile}

    @app.delete("/api/speakers/{member_id}")
    def delete_speaker(member_id: str):
        identity = _require_speaker_identity()
        try:
            removed = identity.registry.delete(member_id)
        except SpeakerError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if (speaker_identity.context.get() or {}).get("member_id") == member_id:
            speaker_identity.context.clear()
        memory_store.add_audit(
            "speaker.delete", f"成员「{removed['display_name']}」（{member_id}）声纹档案删除", "ok", actor="user"
        )
        return {"deleted": True, "speaker": removed}

    @app.post("/api/voice/cache/clear")
    def clear_voice_cache():
        stack = _require_voice_stack()
        cleared = stack.cache.clear()
        return {"cleared": cleared}

    return app


def build_web_app(config) -> FastAPI:
    """运行时装配（CLI 复用同一套组件构造逻辑）。"""
    from .cli import _build_agent, _build_registry
    from .household import HouseholdStore
    from .scheduler import RuleScheduler, chain_ticks
    from .token_budget import TokenBudget
    from .voice.assembly import build_speaker_identity, build_tts_stack

    ha_client = None
    if getattr(config, "device_backend", "") == "homeassistant":
        from .devices.homeassistant import build_ha_registry, get_ha_client

        ha_client = get_ha_client(config)
        registry = build_ha_registry(config, client=ha_client)
    else:
        registry = _build_registry(config)
    memory_store = MemoryStore(config.data_dir / "memory.db")

    # 设备自动发现（阶段 14.5/14.6，仅 HA 后端）：周期刷新兜底 + WebSocket 事件
    # 即时触发（state_changed → 去抖刷新），区域归属来自 WS 注册表查询。
    # 全部 daemon 线程，任一环失败只降级（无区域/退回周期刷新），不影响主流程。
    if (
        getattr(config, "device_backend", "") == "homeassistant"
        and getattr(config, "discovery_enabled", True)
        and ha_client is not None
    ):
        from .devices.discovery import DiscoveryRefresher

        refresher = DiscoveryRefresher(
            ha_client,
            registry,
            interval_seconds=getattr(config, "discovery_refresh_seconds", 300),
            memory_store=memory_store,
            exclusions_path=config.data_dir / "discovery_exclusions.json",
        )
        refresher.start()
        if getattr(config, "discovery_refresh_seconds", 300) <= 0:
            pass  # 用户显式关掉周期刷新：仅 WS 事件触发
        try:
            from .devices.ha_ws import HaWebSocket

            ws_client = HaWebSocket(
                config, on_state_changed=lambda _data: refresher.refresh_now()
            )
            ws_client.start()
            if ws_client.wait_ready(8.0):
                refresher.set_area_provider(ws_client.fetch_area_map)
                refresher.refresh_now()  # 连接即按区域重建一次设备清单
                _log.info("HA WebSocket 就绪：区域信息与新设备即时感知已启用")
            else:
                _log.warning("HA WebSocket 未就绪：区域为空，新设备感知退回周期刷新")
        except Exception:
            _log.exception("HA WebSocket 启动失败（退回周期刷新，不影响其他功能）")

    confirmer = WebConfirmer()
    voice_stack = build_tts_stack(config)
    speaker_identity = build_speaker_identity(config, voice_stack.client)
    # 同一 HouseholdStore 实例贯穿工具层与只读端点，避免同库多连接
    household_store = HouseholdStore(config.data_dir / "household.db")
    agent, rule_engine, executor, routine_runner, household_checker = _build_agent(
        config, registry, memory_store, confirmer=confirmer,
        member_context=speaker_identity.context,
        household_store=household_store,
    )

    # 规划器与 Web 对话共用同一执行器：确认队列一致，防重放缓存也一致
    # （避免"对话刚开的灯"与"任务里的开灯"绕过彼此的重复执行保护）
    planner = PlanRunner.for_cli(config, registry, memory_store, executor=executor)

    # 后台主动检查：不依赖前端轮询，通知落库后由页面定时刷新呈现；
    # extra_tick 链挂例行任务执行器 + 家庭巡检（食材临期/衣物该洗，自保每日一次）
    scheduler = RuleScheduler(
        rule_engine,
        interval_seconds=config.proactive_check_interval_seconds,
        sink=None,
        extra_tick=chain_ticks(routine_runner.run_due, household_checker.run_due),
    )
    scheduler.start()

    # 每日 token 预算展示（用量统计本身在 llm_client 中强制执行）
    token_budget = TokenBudget(memory_store, config.daily_token_budget)

    return create_app(
        config=config,
        registry=registry,
        memory_store=memory_store,
        agent=agent,
        rule_engine=rule_engine,
        confirmer=confirmer,
        planner=planner,
        voice_stack=voice_stack,
        speaker_identity=speaker_identity,
        auth_token=config.web_token or None,
        token_budget=token_budget,
        household_store=household_store,
        executor=executor,
        ha_client=ha_client,
    )


def main() -> int:
    import uvicorn

    from .config import ConfigError, load_config
    from .logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="AI Home Butler Web 界面")
    parser.add_argument("--host", default="127.0.0.1", help="默认仅本机访问，勿暴露公网")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    setup_logging(config.log_dir, config.log_level)
    app = build_web_app(config)
    token = app.state.auth_token
    print(f"AI Home Butler 界面: http://{args.host}:{args.port}/?token={token}")
    if config.web_token:
        print("（访问令牌来自 .env 的 WEB_TOKEN，页面首次打开后无需再带）")
    else:
        print("（访问令牌为本次启动自动生成的随机值；在 .env 设 WEB_TOKEN 可固定）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
