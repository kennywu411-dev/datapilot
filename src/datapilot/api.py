"""DataPilot HTTP 接口（FastAPI）。问数走 SSE 流式返回。

启动：uv run uvicorn datapilot.api:app --reload --port 8000
环境变量：REDIS_URL（默认 redis://localhost:6379/0，连不上自动退回内存）
          DATAPILOT_MODEL（默认 sonnet）
"""

from __future__ import annotations

import json
import os
import random
from contextlib import asynccontextmanager
from functools import cache

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from datapilot import config, tools
from datapilot.service import ChatService
from datapilot.store import make_store

HEARTBEAT_S = 15


class AskRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    db_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=1000)


@cache
def _examples() -> dict[str, list[str]]:
    """每个库挑几道 simple 题当示例问题（固定种子）。"""
    if not config.QUESTIONS_PATH.exists():
        return {}
    qs = json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))
    by_db: dict[str, list[str]] = {}
    for q in qs:
        if q["difficulty"] == "simple" and len(q["question"]) <= 90:
            by_db.setdefault(q["db_id"], []).append(q["question"])
    rng = random.Random(0)
    return {db: rng.sample(v, min(4, len(v))) for db, v in by_db.items()}


def create_app(service: ChatService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            load_dotenv(config.ROOT / ".env")
            store = await make_store(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
            app.state.service = ChatService(
                store, model=os.environ.get("DATAPILOT_MODEL", "sonnet")
            )
        yield

    app = FastAPI(title="DataPilot", lifespan=lifespan)
    if service is not None:
        app.state.service = service  # 测试注入；ASGI 传输不会触发 lifespan
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health():
        svc: ChatService = app.state.service
        return {"store": svc.store.name, "model": svc.model}

    @app.get("/api/databases")
    async def databases():
        return [{"db_id": d, "tables": len(tools.list_tables(d))} for d in tools.list_databases()]

    @app.get("/api/databases/{db_id}/schema")
    async def schema(db_id: str):
        if db_id not in tools.list_databases():
            raise HTTPException(404, f"未知数据库：{db_id}")
        return {
            "db_id": db_id,
            "tables": [tools.describe_table(db_id, t["table"]) for t in tools.list_tables(db_id)],
        }

    @app.get("/api/databases/{db_id}/examples")
    async def examples(db_id: str):
        return _examples().get(db_id, [])

    @app.post("/api/ask")
    async def ask(req: AskRequest):
        svc: ChatService = app.state.service

        async def events():
            async for e in svc.ask(req.session_id, req.db_id, req.question):
                yield {"event": e["event"], "data": json.dumps(e["data"], ensure_ascii=False)}

        # ping：长查询时定期发心跳注释行，防止代理或浏览器判定连接空闲而断开
        return EventSourceResponse(events(), ping=HEARTBEAT_S)

    @app.delete("/api/cache/{db_id}")
    async def clear_cache(db_id: str):
        svc: ChatService = app.state.service
        return {"deleted": await svc.clear_cache(db_id)}

    return app


app = create_app()
