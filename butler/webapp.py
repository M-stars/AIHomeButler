"""Web 界面与 API（阶段 9）：FastAPI 单页仪表盘 + 对话 + 确认队列 + 记忆管理。

安全设计：
  - 与 CLI 共用同一 Agent/工具/安全层；控制类操作的确认通过"确认队列"完成：
    对话请求阻塞等待用户在页面上批准/拒绝（超时按拒绝处理），绝不静默放行；
  - 同一时刻只允许一个对话/任务在执行（防止并发操作同一设备与上下文错乱）；
  - API 无需登录仅限本机使用（默认绑定 127.0.0.1）；文档说明不要暴露到公网。

阶段 12 扩展：音色库管理 API（/api/voices*）与语音合成 API（/api/voice*）。
语音栈为可选装配（voice_stack=None 时这些端点返回 503），不影响原有功能。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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
    if isinstance(exc, VoiceProfileError):
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
) -> FastAPI:
    app = FastAPI(title="AI Home Butler", docs_url=None, redoc_url=None)
    state: Dict[str, Any] = {
        "agent_lock": threading.Lock(),
        "task_lock": threading.Lock(),
        "tts_lock": threading.Lock(),
    }

    def _device_views() -> List[Dict[str, Any]]:
        views = []
        for device in registry.list():
            item = {
                "id": device.info.id,
                "name": device.info.name,
                "type": device.info.device_type,
                "area": device.info.area,
                "available": device.is_available(),
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
        ok = confirmer.respond(confirmation_id, payload.approved)
        if not ok:
            raise HTTPException(status_code=404, detail="确认请求不存在或已处理")
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
    from .voice.assembly import build_tts_stack

    registry = _build_registry(config)
    memory_store = MemoryStore(config.data_dir / "memory.db")
    confirmer = WebConfirmer()
    agent, rule_engine, executor = _build_agent(config, registry, memory_store, confirmer=confirmer)

    # 规划器与 Web 对话共用同一执行器：确认队列一致，防重放缓存也一致
    # （避免"对话刚开的灯"与"任务里的开灯"绕过彼此的重复执行保护）
    planner = PlanRunner.for_cli(config, registry, memory_store, executor=executor)

    # 语音栈：按 TTS_BACKEND 装配（默认 sapi，行为不变；装配不联网，失败只影响合成）
    voice_stack = build_tts_stack(config)

    # 后台主动检查：不依赖前端轮询，通知落库后由页面定时刷新呈现
    scheduler = RuleScheduler(
        rule_engine,
        interval_seconds=config.proactive_check_interval_seconds,
        sink=None,
    )
    scheduler.start()

    return create_app(
        config=config,
        registry=registry,
        memory_store=memory_store,
        agent=agent,
        rule_engine=rule_engine,
        confirmer=confirmer,
        planner=planner,
        voice_stack=voice_stack,
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
    print(f"AI Home Butler 界面: http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
