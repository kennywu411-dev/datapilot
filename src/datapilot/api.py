"""DataPilot HTTP 接口（FastAPI）。问数走 SSE 流式返回。

启动：uv run uvicorn datapilot.api:app --reload --port 8000
环境变量：REDIS_URL（默认 redis://localhost:6379/0，连不上自动退回内存）
          DATAPILOT_MODEL（默认 sonnet；simulated = 不调 API、只模拟延迟，压测用）
          DATAPILOT_RATE_LIMIT（每用户每分钟问数次数，默认 20）
          DATAPILOT_MAX_INFLIGHT（同时调用模型的请求上限，默认 8）
          DATAPILOT_SIM_LATENCY_S（simulated 模式下每次模型调用的延迟，默认 2.0）
          DATAPILOT_CORS_ORIGINS（允许跨域的前端地址，逗号分隔，默认 localhost:3000）
          DATAPILOT_ROLE（默认角色，行列级权限用；请求头 X-Role 可覆盖）

用户识别：请求头 X-User-Id；没有时按客户端 IP。项目没有登录系统，这个头由前端或网关填，
生产环境应换成网关校验过的身份（JWT 等），不能信任客户端自报。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from functools import cache

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from datapilot import config, rls, tools
from datapilot.llm import SimulatedLLM
from datapilot.service import MAX_INFLIGHT, ChatService
from datapilot.store import make_store

log = logging.getLogger(__name__)

HEARTBEAT_S = 15
RATE_LIMIT_PER_MIN = 20
RATE_WINDOW_S = 60
_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NO_KEY_MESSAGE = "未设置 ANTHROPIC_API_KEY，请在 .env 中填写后重启"
INTERNAL_ERROR_MESSAGE = "服务内部错误，请查看服务端日志"


def api_key_missing() -> bool:
    """SDK 从 ANTHROPIC_API_KEY（或 ANTHROPIC_AUTH_TOKEN）读凭据；两者都没有时问数必然失败。"""
    return not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def is_auth_error(e: BaseException) -> bool:
    """没配 key 时 SDK 在发请求前抛 TypeError；key 错误时服务端返回 401。"""
    if isinstance(e, anthropic.AuthenticationError):
        return True
    return isinstance(e, TypeError) and "Could not resolve authentication method" in str(e)


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


def role_of(request: Request) -> str | None:
    """角色：请求头 X-Role，没有时用 DATAPILOT_ROLE。和 X-User-Id 一样是客户端自报，
    项目没有登录系统；生产环境必须由网关根据登录身份填写，不能信任客户端。"""
    role = request.headers.get("x-role") or os.environ.get("DATAPILOT_ROLE") or None
    if role is not None and not _USER_ID.fullmatch(role):
        raise HTTPException(400, "X-Role 格式不对")
    return role


def user_id_of(request: Request) -> str:
    """X-User-Id 优先，没有时按客户端 IP；格式不对直接拒绝（它会拼进存储 key）。"""
    uid = request.headers.get("x-user-id")
    if uid is None:
        host = request.client.host if request.client else "unknown"
        return f"ip-{host}"
    if not _USER_ID.fullmatch(uid):
        raise HTTPException(400, "X-User-Id 只能包含字母、数字、下划线和连字符，最长 64 位")
    return uid


def _make_service(store) -> ChatService:
    model = os.environ.get("DATAPILOT_MODEL", "sonnet")
    max_inflight = int(os.environ.get("DATAPILOT_MAX_INFLIGHT", MAX_INFLIGHT))
    if model == "simulated":
        latency = float(os.environ.get("DATAPILOT_SIM_LATENCY_S", "2.0"))
        return ChatService(
            store,
            model=model,
            llm_factory=lambda: SimulatedLLM(latency),
            max_inflight=max_inflight,
        )
    return ChatService(store, model=model, max_inflight=max_inflight)


def create_app(
    service: ChatService | None = None, rate_limit_per_min: int | None = None
) -> FastAPI:
    if rate_limit_per_min is None:
        rate_limit_per_min = int(os.environ.get("DATAPILOT_RATE_LIMIT", RATE_LIMIT_PER_MIN))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            load_dotenv(config.ROOT / ".env")
            if os.environ.get("DATAPILOT_MODEL", "sonnet") != "simulated" and api_key_missing():
                log.warning(
                    "未设置 ANTHROPIC_API_KEY：服务可以启动，但问数会失败。"
                    "请复制 .env.example 为 .env 并填写后重启"
                )
            store = await make_store(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
            app.state.service = _make_service(store)
        yield

    app = FastAPI(title="DataPilot", lifespan=lifespan)
    if service is not None:
        app.state.service = service  # 测试注入；ASGI 传输不会触发 lifespan
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            o.strip()
            for o in os.environ.get(
                "DATAPILOT_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
            ).split(",")
            if o.strip()
        ],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health():
        svc: ChatService = app.state.service
        return {
            "store": svc.store.name,
            "model": svc.model,
            "rate_limit_per_min": rate_limit_per_min,
            "max_inflight": svc.max_inflight,
        }

    @app.get("/api/databases")
    async def databases():
        return [{"db_id": d, "tables": len(tools.list_tables(d))} for d in tools.list_databases()]

    @app.get("/api/databases/{db_id}/schema")
    async def schema(db_id: str, request: Request):
        if db_id not in tools.list_databases():
            raise HTTPException(404, f"未知数据库：{db_id}")
        role = role_of(request)
        try:
            tables = [
                tools.describe_table(db_id, t["table"], role) for t in tools.list_tables(db_id)
            ]
        except ValueError as e:
            raise HTTPException(403, str(e)) from e
        return {"db_id": db_id, "role": role, "tables": tables}

    @app.get("/api/databases/{db_id}/roles")
    async def roles(db_id: str):
        """该库定义的角色（演示用：前端据此显示角色切换）。"""
        return [{"role": k, "label": v} for k, v in rls.roles_of(db_id).items()]

    @app.get("/api/databases/{db_id}/examples")
    async def examples(db_id: str):
        return _examples().get(db_id, [])

    async def check_rate(svc: ChatService, user_id: str) -> None:
        """固定窗口：每个用户每分钟一个计数 key。缓存命中也计数，限制的是请求频率而不只是花费。"""
        now = time.time()
        window = int(now // RATE_WINDOW_S)
        n = await svc.store.incr(f"dp:rl:{user_id}:{window}", RATE_WINDOW_S)
        if n > rate_limit_per_min:
            retry = max(1, int(RATE_WINDOW_S - now % RATE_WINDOW_S))
            raise HTTPException(
                429,
                f"提问太频繁：每分钟最多 {rate_limit_per_min} 次，请 {retry} 秒后再试",
                headers={"Retry-After": str(retry)},
            )

    @app.post("/api/ask")
    async def ask(req: AskRequest, request: Request):
        svc: ChatService = app.state.service
        user_id = user_id_of(request)
        role = role_of(request)
        await check_rate(svc, user_id)

        def sse(event: str, data: dict) -> dict:
            return {"event": event, "data": json.dumps(data, ensure_ascii=False)}

        async def events():
            # 流已经是 200 了，异常只能变成 error 事件发出去，不能让前端收到一段空流或 traceback
            try:
                async for e in svc.ask(
                    req.session_id, req.db_id, req.question, user_id=user_id, role=role
                ):
                    yield sse(e["event"], e["data"])
            except Exception as e:
                if is_auth_error(e):
                    message = NO_KEY_MESSAGE
                else:
                    log.exception("问数失败：%s", e)
                    message = INTERNAL_ERROR_MESSAGE
                yield sse("error", {"message": message})
                yield sse(
                    "done", {"cached": False, "latency_s": 0.0, "cost_usd": 0.0, "model": svc.model}
                )

        # ping：长查询时定期发心跳注释行，防止代理或浏览器判定连接空闲而断开
        return EventSourceResponse(events(), ping=HEARTBEAT_S)

    @app.delete("/api/cache/{db_id}")
    async def clear_cache(db_id: str):
        svc: ChatService = app.state.service
        return {"deleted": await svc.clear_cache(db_id)}

    return app


app = create_app()
