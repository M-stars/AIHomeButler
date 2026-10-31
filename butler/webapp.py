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
import os
import secrets
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .devices.base import DeviceError
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


class WebConfirmer:
    """Web 端确认器：把确认请求放入队列，等待页面批准/拒绝；超时按拒绝处理。"""

    def __init__(self, timeout_seconds: float = 120.0) -> None:
        self._timeout = float(timeout_seconds)
        self._lock = threading.Lock()
        self._pending: List[Dict[str, Any]] = []

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
        resolved = item["event"].wait(self._timeout)
        with self._lock:
            if item in self._pending:
                self._pending.remove(item)
        if not resolved and not item["event"].is_set():
            # 超时且确实无人应答才按拒绝处理；若应答恰好落在超时临界点
            # （wait 已返回但 respond 已写入结果），以写入结果为准（P2 竞态修复）
            item["approved"] = False
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
) -> FastAPI:
    app = FastAPI(title="AI Home Butler", docs_url=None, redoc_url=None)
    # 访问令牌：未显式传入（或配置为空）时自动生成随机值，启动时打印到控制台
    app.state.auth_token = auth_token or secrets.token_urlsafe(24)

    # ---- 访问鉴权（评审 4.3）：除静态外壳页外，所有请求必须携带正确令牌 ----
    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        if request.url.path != "/":
            supplied = (
                request.headers.get("X-Butler-Token")
                or request.query_params.get("token")
            )
            if not supplied:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    supplied = auth_header[len("Bearer "):]
            if not supplied or not secrets.compare_digest(
                supplied, app.state.auth_token
            ):
                return Response(
                    content='{"detail":"未授权：缺少或错误的访问令牌"}',
                    status_code=401,
                    media_type="application/json",
                )
        return await call_next(request)

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
        }

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
        body["service_healthy"] = voice_stack.client.health_check(timeout_seconds=2.0)
        return body

    @app.post("/api/speakers")
    async def enroll_speaker(
        display_name: str = Form(),
        consent_granted: bool = Form(),
        voice_id: str = Form(default=""),
        wav: UploadFile = File(),
    ):
        identity = _require_speaker_identity()
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
    from .scheduler import RuleScheduler
    from .token_budget import TokenBudget
    from .voice.assembly import build_speaker_identity, build_tts_stack

    registry = _build_registry(config)
    memory_store = MemoryStore(config.data_dir / "memory.db")

    # 设备自动发现（阶段 14.5/14.6，仅 HA 后端）：周期刷新兜底 + WebSocket 事件
    # 即时触发（state_changed → 去抖刷新），区域归属来自 WS 注册表查询。
    # 全部 daemon 线程，任一环失败只降级（无区域/退回周期刷新），不影响主流程。
    if (
        getattr(config, "device_backend", "") == "homeassistant"
        and getattr(config, "discovery_enabled", True)
    ):
        from .devices.discovery import DiscoveryRefresher
        from .devices.homeassistant import get_ha_client

        refresher = DiscoveryRefresher(
            get_ha_client(config),
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
    agent, rule_engine, executor, routine_runner = _build_agent(
        config, registry, memory_store, confirmer=confirmer,
        member_context=speaker_identity.context,
    )

    # 规划器与 Web 对话共用同一执行器：确认队列一致，防重放缓存也一致
    # （避免"对话刚开的灯"与"任务里的开灯"绕过彼此的重复执行保护）
    planner = PlanRunner.for_cli(config, registry, memory_store, executor=executor)

    # 后台主动检查：不依赖前端轮询，通知落库后由页面定时刷新呈现；
    # extra_tick 挂例行任务执行器——到点自动执行预授权动作，结果同样落库
    scheduler = RuleScheduler(
        rule_engine,
        interval_seconds=config.proactive_check_interval_seconds,
        sink=None,
        extra_tick=routine_runner.run_due,
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
