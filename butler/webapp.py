"""Web 界面与 API（阶段 9）：FastAPI 单页仪表盘 + 对话 + 确认队列 + 记忆管理。

安全设计：
  - 与 CLI 共用同一 Agent/工具/安全层；控制类操作的确认通过"确认队列"完成：
    对话请求阻塞等待用户在页面上批准/拒绝（超时按拒绝处理），绝不静默放行；
  - 同一时刻只允许一个对话/任务在执行（防止并发操作同一设备与上下文错乱）；
  - API 无需登录仅限本机使用（默认绑定 127.0.0.1）；文档说明不要暴露到公网。
"""
from __future__ import annotations

import argparse
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .devices.base import DeviceError
from .devices.registry import DeviceRegistry
from .memory.store import MemoryStore
from .planner import PlanRunner
from .proactive import RuleEngine
from .tools.executor import ToolExecutor

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


def create_app(
    *,
    config,
    registry: DeviceRegistry,
    memory_store: MemoryStore,
    agent,
    rule_engine: RuleEngine,
    confirmer: WebConfirmer,
    planner: Optional[PlanRunner] = None,
) -> FastAPI:
    app = FastAPI(title="AI Home Butler", docs_url=None, redoc_url=None)
    state: Dict[str, Any] = {
        "agent_lock": threading.Lock(),
        "task_lock": threading.Lock(),
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

    return app


def build_web_app(config) -> FastAPI:
    """运行时装配（CLI 复用同一套组件构造逻辑）。"""
    from .cli import _build_agent, _build_registry
    from .scheduler import RuleScheduler

    registry = _build_registry(config)
    memory_store = MemoryStore(config.data_dir / "memory.db")
    confirmer = WebConfirmer()
    agent, rule_engine, executor = _build_agent(config, registry, memory_store, confirmer=confirmer)

    # 规划器与 Web 对话共用同一执行器：确认队列一致，防重放缓存也一致
    # （避免"对话刚开的灯"与"任务里的开灯"绕过彼此的重复执行保护）
    planner = PlanRunner.for_cli(config, registry, memory_store, executor=executor)

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
