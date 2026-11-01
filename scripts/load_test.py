"""并发压测：对真实运行的 uvicorn 发真实 HTTP，读完整个 SSE 流，测服务端自身的并发表现。

模型用 SimulatedLLM 代替（服务端以 DATAPILOT_MODEL=simulated 启动），每次模型调用只 sleep
固定时长，花费为 0。所以这里的数字反映的是事件循环、存储、限流、并发保护，
**不是**真实模型的延迟分布。

三个场景：
- hit：预热一题后，各并发档发 200 个同样的问题，全部命中缓存；每个请求用不同用户 id，避开限流
- miss：问题加随机后缀保证不命中，走完整流程（生成 SQL → 执行 → 检查 → 回答），
  看信号量排队与繁忙拒绝
- ratelimit：同一用户顺序发 25 次，预期前 N 次 200、之后 429

用法（先另开终端启动服务）：
  DATAPILOT_MODEL=simulated DATAPILOT_SIM_LATENCY_S=1.5 REDIS_URL=redis://localhost:6379/2 \\
    uv run uvicorn datapilot.api:app --port 8010
  uv run python scripts/load_test.py --base http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datapilot import config  # noqa: E402
from evals.report import pct  # noqa: E402

DB_ID = "california_schools"
QUESTION = "How many schools are there in total?"
HIT_LEVELS, HIT_REQUESTS = [1, 10, 50, 100], 200
MISS_LEVELS = [1, 8, 16, 50]
RATE_REQUESTS = 25
_EVENT = re.compile(r"^event: ?(\w+)", re.M)


async def one(client: httpx.AsyncClient, user: str, question: str) -> dict:
    """发一次问数并读完 SSE。outcome：ok / busy / limited / error。"""
    body = {"session_id": f"s-{uuid.uuid4().hex[:8]}", "db_id": DB_ID, "question": question}
    start = time.perf_counter()
    first = None
    async with client.stream("POST", "/api/ask", json=body, headers={"X-User-Id": user}) as r:
        if r.status_code != 200:
            await r.aread()
            outcome = "limited" if r.status_code == 429 else "error"
            return {"status": r.status_code, "outcome": outcome, "total_s": 0.0, "first_s": 0.0}
        text = ""
        async for chunk in r.aiter_text():
            if first is None:
                first = time.perf_counter() - start
            text += chunk
    total = time.perf_counter() - start
    kinds = _EVENT.findall(text)
    if "answer" in kinds:
        outcome = "ok"
    elif "繁忙" in text:
        outcome = "busy"
    else:
        outcome = "error"
    cached = '"cached": true' in text
    return {"status": 200, "outcome": outcome, "cached": cached, "total_s": total, "first_s": first}


async def run_level(client: httpx.AsyncClient, concurrency: int, reqs: list[tuple[str, str]]):
    sem = asyncio.Semaphore(concurrency)

    async def guarded(user: str, q: str) -> dict:
        async with sem:
            return await one(client, user, q)

    start = time.perf_counter()
    rows = await asyncio.gather(*(guarded(u, q) for u, q in reqs))
    wall = time.perf_counter() - start
    ok = [r for r in rows if r["outcome"] == "ok"]
    tot = sorted(r["total_s"] for r in ok)
    first = sorted(r["first_s"] for r in ok)
    return {
        "concurrency": concurrency,
        "requests": len(rows),
        "ok": len(ok),
        "busy": sum(r["outcome"] == "busy" for r in rows),
        "errors": sum(r["outcome"] == "error" for r in rows),
        "cached": sum(bool(r.get("cached")) for r in ok),
        "p50_s": pct(tot, 0.5),
        "p95_s": pct(tot, 0.95),
        "first_event_p50_s": pct(first, 0.5),
        "wall_s": wall,
        "ok_per_s": len(ok) / wall,
    }


async def scenario_hit(client: httpx.AsyncClient) -> list[dict]:
    warm = await one(client, "warmup", QUESTION)
    assert warm["outcome"] == "ok", f"预热失败：{warm}"
    out = []
    for c in HIT_LEVELS:
        reqs = [(f"hit-{c}-{i}", QUESTION) for i in range(HIT_REQUESTS)]
        out.append(await run_level(client, c, reqs))
        print_row("hit", out[-1])
    return out


async def scenario_miss(client: httpx.AsyncClient) -> list[dict]:
    out = []
    for c in MISS_LEVELS:
        n = max(8, c * 3)
        reqs = [(f"miss-{c}-{i}", f"{QUESTION} #{uuid.uuid4().hex[:8]}") for i in range(n)]
        out.append(await run_level(client, c, reqs))
        print_row("miss", out[-1])
    return out


async def scenario_ratelimit(client: httpx.AsyncClient, limit: int) -> dict:
    # 固定窗口：离窗口结束太近时等到下一个窗口，否则计数会被切成两段
    remaining = 60 - time.time() % 60
    if remaining < 15:
        print(f"离窗口结束 {remaining:.0f}s，等到下一个窗口")
        await asyncio.sleep(remaining + 0.5)
    user = f"rl-{uuid.uuid4().hex[:6]}"
    statuses = [(await one(client, user, QUESTION))["status"] for _ in range(RATE_REQUESTS)]
    res = {
        "limit_per_min": limit,
        "requests": RATE_REQUESTS,
        "ok_200": statuses.count(200),
        "limited_429": statuses.count(429),
        "first_429_at": statuses.index(429) + 1 if 429 in statuses else None,
    }
    print(f"ratelimit: {res}")
    return res


def print_row(name: str, r: dict) -> None:
    print(
        f"{name:5} c={r['concurrency']:>3} n={r['requests']:>3} ok={r['ok']:>3} "
        f"busy={r['busy']:>3} err={r['errors']:>2} P50={r['p50_s']:.3f}s "
        f"P95={r['p95_s']:.3f}s 吞吐={r['ok_per_s']:.1f}/s"
    )


async def main_async(base: str) -> None:
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as client:
        health = (await client.get("/api/health")).json()
        assert health["model"] == "simulated", f"服务端不是 simulated 模式，会花真钱：{health}"
        await client.delete(f"/api/cache/{DB_ID}")  # 从冷缓存开始
        print(f"服务端：{health}")

        run_id = f"load-{time.strftime('%Y%m%d-%H%M%S')}"
        summary = {
            "run_id": run_id,
            "server": health,
            "note": "模型为 SimulatedLLM（固定 sleep），数字反映服务端自身，不代表真实模型延迟",
            "db_id": DB_ID,
            "hit": await scenario_hit(client),
            "miss": await scenario_miss(client),
            "ratelimit": await scenario_ratelimit(client, health["rate_limit_per_min"]),
        }
    out = config.RUNS_DIR / run_id
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"已写入 {out / 'summary.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    args = ap.parse_args()
    asyncio.run(main_async(args.base))


if __name__ == "__main__":
    main()
