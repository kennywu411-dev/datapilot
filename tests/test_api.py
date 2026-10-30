"""FastAPI 接口：用 ASGI 传输直接调用，ScriptedLLM + 内存存储，不花钱。"""

import json

import httpx
import pytest

from datapilot.api import create_app
from datapilot.baseline import SQLAnswer
from datapilot.llm import ScriptedLLM
from datapilot.service import ChatService
from datapilot.store import MemoryStore


@pytest.fixture
def client(shop_db_dir):
    llm = ScriptedLLM([SQLAnswer(sql="SELECT count(*) AS n FROM users"), "共 3 人。"])
    app = create_app(service=ChatService(MemoryStore(), llm_factory=lambda: llm))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\r\n\r\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "event" in fields:
            events.append({"event": fields["event"], "data": json.loads(fields["data"])})
    return events


async def test_health_and_databases(client):
    async with client as c:
        h = (await c.get("/api/health")).json()
        assert h["store"] == "memory"
        dbs = (await c.get("/api/databases")).json()
        assert dbs == [{"db_id": "shop", "tables": 3}]


async def test_schema(client):
    async with client as c:
        s = (await c.get("/api/databases/shop/schema")).json()
        users = next(t for t in s["tables"] if t["table"] == "users")
        assert [c["name"] for c in users["columns"]][:2] == ["id", "name"]
        assert (await c.get("/api/databases/nope/schema")).status_code == 404


async def test_ask_streams_sse(client):
    async with client as c:
        r = await c.post(
            "/api/ask", json={"session_id": "s1", "db_id": "shop", "question": "多少人"}
        )
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        kinds = [e["event"] for e in events]
        assert "sql" in kinds and "table" in kinds and kinds[-1] == "done"
        assert next(e for e in events if e["event"] == "chart")["data"]["type"] == "kpi"


async def test_ask_reports_missing_api_key_as_error_event(shop_db_dir):
    def no_key():
        raise TypeError(
            '"Could not resolve authentication method. Expected one of api_key, auth_token, '
            'or credentials to be set."'
        )

    app = create_app(service=ChatService(MemoryStore(), llm_factory=no_key))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/ask", json={"session_id": "s1", "db_id": "shop", "question": "多少人"}
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert [e["event"] for e in events] == ["error", "done"]
        assert "ANTHROPIC_API_KEY" in events[0]["data"]["message"]


async def test_ask_validates_input(client):
    async with client as c:
        r = await c.post("/api/ask", json={"session_id": "s1", "db_id": "shop", "question": ""})
        assert r.status_code == 422


async def test_clear_cache(client):
    async with client as c:
        r = await c.delete("/api/cache/shop")
        assert r.json() == {"deleted": 0}


def make_client(limit_per_min=2):
    def factory():
        return ScriptedLLM([SQLAnswer(sql="SELECT count(*) AS n FROM users"), "共 3 人。"] * 10)

    svc = ChatService(MemoryStore(), llm_factory=factory)
    app = create_app(service=svc, rate_limit_per_min=limit_per_min)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), svc


async def ask(c, user=None, question="多少人"):
    headers = {"X-User-Id": user} if user else {}
    body = {"session_id": "s1", "db_id": "shop", "question": question}
    return await c.post("/api/ask", json=body, headers=headers)


async def test_rate_limit_per_user_returns_429(shop_db_dir):
    client, _ = make_client(limit_per_min=2)
    async with client as c:
        assert (await ask(c, "alice")).status_code == 200
        assert (await ask(c, "alice")).status_code == 200
        r = await ask(c, "alice")
        assert r.status_code == 429
        assert 1 <= int(r.headers["Retry-After"]) <= 60
        assert "频繁" in r.json()["detail"]
        assert (await ask(c, "bob")).status_code == 200  # 别的用户不受影响


async def test_user_header_scopes_session(shop_db_dir):
    client, svc = make_client()
    async with client as c:
        await ask(c, "alice")
        await ask(c)  # 没有用户头：按客户端 IP 识别
    assert await svc.store.get_json("dp:session:alice:s1")
    assert await svc.store.get_json("dp:session:ip-127.0.0.1:s1")


async def test_invalid_user_header_is_rejected(shop_db_dir):
    client, _ = make_client()
    async with client as c:
        assert (await ask(c, "bad user:id")).status_code == 400
