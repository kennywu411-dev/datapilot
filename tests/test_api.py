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


async def test_ask_validates_input(client):
    async with client as c:
        r = await c.post("/api/ask", json={"session_id": "s1", "db_id": "shop", "question": ""})
        assert r.status_code == 422


async def test_clear_cache(client):
    async with client as c:
        r = await c.delete("/api/cache/shop")
        assert r.json() == {"deleted": 0}
