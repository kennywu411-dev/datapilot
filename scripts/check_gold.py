"""D1 评测自检：预跑 500 道标准 SQL，并做两项自检。

1. 标准 SQL 自身：执行状态、行数、耗时，标出报错 / 空结果 / 超时的题；
2. 自检 0（标准对标准）：pred = gold，官方口径下慢题会因共用 30 秒预算判错；
3. 自检 A（打乱行顺序）：pred = SELECT * FROM (gold) ORDER BY RANDOM()，应 100% 判对；
4. 自检 B（假 Agent）：pred 恒为 SELECT 1，应接近 0%。

结果写入 runs/<run_id>/gold_check.jsonl 和 summary.json。
用法：uv run python scripts/check_gold.py [--workers 8]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

from datapilot.ex import evaluate_one
from datapilot.sql_guard import run_readonly

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def db_path(db_id: str) -> Path:
    return DATA / "dev_databases" / db_id / f"{db_id}.sqlite"


def shuffled(gold: str) -> str:
    return f"SELECT * FROM ({gold.strip().rstrip(';')}) ORDER BY RANDOM()"


def check(q: dict) -> dict:
    path, gold = db_path(q["db_id"]), q["SQL"]
    row = {"question_id": q["question_id"], "db_id": q["db_id"], "difficulty": q["difficulty"]}
    try:
        res = run_readonly(path, gold, max_rows=None, skip_guard=True)
        row.update(gold_status="ok", gold_rows=len(res.rows), gold_s=round(res.elapsed_s, 3))
        row["gold_all_null"] = bool(res.rows) and all(v is None for r in res.rows for v in r)
    except TimeoutError:
        row.update(gold_status="timeout", gold_rows=None, gold_s=None)
    except sqlite3.Error as e:
        row.update(gold_status="error", gold_rows=None, gold_s=None, gold_error=str(e))
    row["gold_self_ok"] = evaluate_one(path, gold, gold).correct
    row["shuffle_ok"] = evaluate_one(path, shuffled(gold), gold).correct
    row["select1_ok"] = evaluate_one(path, "SELECT 1", gold).correct
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    questions = json.loads((DATA / "mini_dev_sqlite.json").read_text(encoding="utf-8"))
    run_id = f"gold-check-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir = ROOT / "runs" / run_id
    out_dir.mkdir(parents=True)

    start = time.monotonic()
    with ProcessPoolExecutor(args.workers) as pool:
        rows = list(pool.map(check, questions))
    wall = time.monotonic() - start

    with (out_dir / "gold_check.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(rows)
    status = Counter(r["gold_status"] for r in rows)
    empty = [r["question_id"] for r in rows if r["gold_rows"] == 0]
    all_null = [r["question_id"] for r in rows if r.get("gold_all_null")]
    shuffle_fail = [r["question_id"] for r in rows if not r["shuffle_ok"]]
    select1_hit = [r["question_id"] for r in rows if r["select1_ok"]]
    times = sorted(r["gold_s"] for r in rows if r["gold_s"] is not None)
    summary = {
        "run_id": run_id,
        "questions": n,
        "gold_status": dict(status),
        "gold_errors": [r["question_id"] for r in rows if r["gold_status"] != "ok"],
        "gold_empty": empty,
        "gold_all_null": all_null,
        "gold_p50_s": times[len(times) // 2],
        "gold_max_s": times[-1],
        "selfcheck_gold_self_acc": sum(r["gold_self_ok"] for r in rows) / n,
        "selfcheck_gold_self_fail": [r["question_id"] for r in rows if not r["gold_self_ok"]],
        "selfcheck_shuffle_acc": (n - len(shuffle_fail)) / n,
        "selfcheck_shuffle_fail": shuffle_fail,
        "selfcheck_select1_acc": len(select1_hit) / n,
        "selfcheck_select1_hit": select1_hit,
        "wall_s": round(wall, 1),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
