"""查询缓存实测：构造含重复问题的请求流，测命中率、命中 / 未命中延迟、节省的 API 费用。

请求分布是**构造的**，不是真实用户数据：
- 从 BIRD 题中按固定种子取 UNIQUE 道不重复问题；
- 再加入 REPEAT 次重复：一半原样重复，一半改动标点 / 空格 / 大小写 / 全角（检验问题规范化）；
- 打乱顺序，保证每次重复都出现在首次提问之后；每个请求用新会话（不带追问上下文）。

用法：uv run python scripts/cache_bench.py --yes   （默认 Sonnet，预计约 $1.3，硬上限 $1.6）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datapilot import config  # noqa: E402
from datapilot.service import ChatService  # noqa: E402
from datapilot.store import make_store  # noqa: E402
from evals.report import pct  # noqa: E402

UNIQUE, REPEAT, SEED = 140, 60, 0
MAX_COST = 1.6


def perturb(q: str, rng: random.Random) -> str:
    """改写成规范化后等价的问法：去问号、多空格、大小写、全角。"""
    choice = rng.randrange(4)
    if choice == 0:
        return q.rstrip("?？") + "？"
    if choice == 1:
        return "  " + q.replace(" ", "  ") + " "
    if choice == 2:
        return q.upper()
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" and c.isdigit() else c for c in q)


def build_requests() -> list[dict]:
    rng = random.Random(SEED)
    qs = json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))
    pool = [
        q for q in qs if q["difficulty"] != "challenging" and q["question_id"] not in (518, 701)
    ]
    uniques = rng.sample(pool, UNIQUE)
    reqs = [{"db_id": q["db_id"], "question": q["question"], "kind": "first"} for q in uniques]
    for i in range(REPEAT):
        src = rng.choice(uniques)
        variant = src["question"] if i % 2 == 0 else perturb(src["question"], rng)
        reqs.append(
            {"db_id": src["db_id"], "question": variant, "kind": "repeat", "src": src["question"]}
        )
    # 打乱，但保证重复出现在首次之后：先打乱首问，再把每个重复插到它的首问之后的随机位置
    firsts = reqs[:UNIQUE]
    rng.shuffle(firsts)
    order = list(firsts)
    for r in reqs[UNIQUE:]:
        pos = next(
            i for i, x in enumerate(order) if x["kind"] == "first" and x["question"] == r["src"]
        )
        order.insert(rng.randint(pos + 1, len(order)), r)
    # 核心前提：每次重复都排在它的首问之后，否则必然未命中，命中率被低估
    first_pos = {r["question"]: i for i, r in enumerate(order) if r["kind"] == "first"}
    assert len(order) == UNIQUE + REPEAT
    assert all(first_pos[r["src"]] < i for i, r in enumerate(order) if r["kind"] == "repeat")
    return order


async def main_async(model: str) -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    store = await make_store("redis://localhost:6379/0")
    svc = ChatService(store, model=model)
    for db in {r["db_id"] for r in build_requests()}:
        await svc.clear_cache(db)  # 从冷缓存开始

    run_id = f"cache-bench-{time.strftime('%Y%m%d-%H%M%S')}"
    out = config.RUNS_DIR / run_id
    out.mkdir(parents=True)
    rows, spent = [], 0.0
    for i, req in enumerate(build_requests()):
        if spent >= MAX_COST:
            print(f"达到花费上限 ${MAX_COST}，停止")
            break
        start = time.perf_counter()
        done, error = None, None
        async for e in svc.ask(f"bench-{i}", req["db_id"], req["question"]):
            if e["event"] == "done":
                done = e["data"]
            elif e["event"] == "error":
                error = e["data"]["message"]
        row = {
            **req,
            "cached": done["cached"],
            "latency_s": time.perf_counter() - start,
            "cost_usd": done["cost_usd"],
            "error": error,
        }
        spent += row["cost_usd"]
        rows.append(row)
        tag = "HIT " if row["cached"] else "miss"
        print(f"[{i + 1}] {tag} {row['latency_s']:.2f}s ${row['cost_usd']:.4f}")
    (out / "results.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))

    hits = [r for r in rows if r["cached"]]
    misses = [r for r in rows if not r["cached"]]
    repeats = [r for r in rows if r["kind"] == "repeat"]
    miss_cost = sum(r["cost_usd"] for r in misses) / max(1, len(misses))
    hl, ml = sorted(r["latency_s"] for r in hits), sorted(r["latency_s"] for r in misses)
    summary = {
        "run_id": run_id,
        "model": model,
        "store": store.name,
        "requests": len(rows),
        "distribution": f"构造：{UNIQUE} 道不重复 + {REPEAT} 次重复（一半原样、一半改写）",
        "hit_rate": len(hits) / len(rows),
        "repeat_hit_rate": sum(r["cached"] for r in repeats) / max(1, len(repeats)),
        "repeat_variant_hit_rate": sum(r["cached"] for r in repeats if r["question"] != r["src"])
        / max(1, sum(1 for r in repeats if r["question"] != r["src"])),
        "hit_latency_p50": pct(hl, 0.5),
        "hit_latency_p95": pct(hl, 0.95),
        "miss_latency_p50": pct(ml, 0.5),
        "miss_latency_p95": pct(ml, 0.95),
        "cost_actual": spent,
        "cost_without_cache_est": miss_cost * len(rows),
        "errors": sum(1 for r in rows if r["error"]),
    }
    summary["cost_saved_pct"] = 1 - summary["cost_actual"] / summary["cost_without_cache_est"]
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    reqs = build_requests()
    print(f"{len(reqs)} 次请求（{UNIQUE} 不重复 + {REPEAT} 重复），模型 {args.model}")
    print(f"硬上限 ${MAX_COST}")
    if not args.yes:
        print("加 --yes 确认后执行。")
        return
    asyncio.run(main_async(args.model))


if __name__ == "__main__":
    main()
