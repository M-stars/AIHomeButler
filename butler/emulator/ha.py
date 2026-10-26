"""本地 Home Assistant REST API 模拟器。

用途：
  1. 在没有真实 HA 的开发/测试环境中，提供与真实 HA REST API 相同语义的端点，
     让 HomeAssistantAdapter 得到真实 HTTP 链路级别的集成测试；
  2. 当真实 HA 部署后（Docker 就绪），仅需改 .env 指向真实实例，代码零改动。

只实现本项目用到的端点子集：GET /api/states、GET /api/states/{entity_id}、
POST /api/services/{domain}/{service}。启动：python -m butler.emulator --port 8123
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _default_entities() -> Dict[str, Dict[str, Any]]:
    return {
        "sensor.living_room_temp": {
            "entity_id": "sensor.living_room_temp",
            "state": "22.5",
            "attributes": {"unit_of_measurement": "°C", "friendly_name": "客厅温度"},
            "last_updated": _now_iso(),
            "last_changed": _now_iso(),
        },
        "sensor.living_room_humidity": {
            "entity_id": "sensor.living_room_humidity",
            "state": "48.0",
            "attributes": {"unit_of_measurement": "%", "friendly_name": "客厅湿度"},
            "last_updated": _now_iso(),
            "last_changed": _now_iso(),
        },
        "light.living_room": {
            "entity_id": "light.living_room",
            "state": "off",
            "attributes": {"friendly_name": "客厅灯", "brightness": 204},
            "last_updated": _now_iso(),
            "last_changed": _now_iso(),
        },
        "light.bedroom": {
            "entity_id": "light.bedroom",
            "state": "off",
            "attributes": {"friendly_name": "卧室灯"},
            "last_updated": _now_iso(),
            "last_changed": _now_iso(),
        },
        "fan.bedroom": {
            "entity_id": "fan.bedroom",
            "state": "off",
            "attributes": {"friendly_name": "卧室风扇", "percentage": 66},
            "last_updated": _now_iso(),
            "last_changed": _now_iso(),
        },
        "light.broken": {
            "entity_id": "light.broken",
            "state": "unavailable",
            "attributes": {"friendly_name": "故障灯（模拟离线）"},
            "last_updated": _now_iso(),
            "last_changed": _now_iso(),
        },
    }


def create_emulator_app(
    entities: Optional[Dict[str, Dict[str, Any]]] = None,
    require_token: Optional[str] = None,
) -> FastAPI:
    """require_token 提供时模拟真实 HA 的 Bearer 令牌校验（不匹配返回 401）。"""
    app = FastAPI(title="HA Butler Emulator", docs_url=None, redoc_url=None)
    state = {"entities": entities if entities is not None else _default_entities()}

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if require_token is not None and request.url.path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {require_token}":
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=401, content={"message": "Unauthorized"}
                )
        return await call_next(request)

    def _touch(entity_id: str) -> None:
        entity = state["entities"][entity_id]
        entity["last_updated"] = _now_iso()
        entity["last_changed"] = _now_iso()

    @app.get("/api/")
    def api_root():
        return {"message": "API running."}

    @app.get("/api/states")
    def all_states():
        with _LOCK:
            return list(state["entities"].values())

    @app.get("/api/states/{entity_id}")
    def one_state(entity_id: str):
        with _LOCK:
            entity = state["entities"].get(entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail="Entity not found.")
        return entity

    @app.post("/api/services/{domain}/{service}")
    async def call_service(domain: str, service: str, request: Request):
        body = await request.json()
        entity_ids = body.get("entity_id")
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        results = []
        with _LOCK:
            for entity_id in entity_ids or []:
                entity = state["entities"].get(entity_id)
                if entity is None:
                    raise HTTPException(status_code=404, detail="Entity not found.")
                _apply_service(state["entities"], entity, domain, service, body)
                _touch(entity_id)
                results.append(dict(entity))
        return results

    def _apply_service(entities, entity, domain, service, data) -> None:
        # 与真实 HA 行为对齐的最小实现
        if domain == "light":
            if service == "turn_on":
                entity["state"] = "on"
                if "brightness" in data:
                    entity["attributes"]["brightness"] = int(data["brightness"])
            elif service == "turn_off":
                entity["state"] = "off"
        elif domain == "fan":
            if service == "turn_on":
                entity["state"] = "on"
                if "percentage" in data:
                    entity["attributes"]["percentage"] = int(data["percentage"])
            elif service == "turn_off":
                entity["state"] = "off"
        # 未知 domain/service：真实 HA 会返回 404/400，这里同样拒绝
        else:
            raise HTTPException(status_code=404, detail="Service not found.")

    return app


def main() -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Home Assistant REST API 模拟器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    uvicorn.run(create_emulator_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
