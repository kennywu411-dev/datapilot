"""统计 BIRD Mini-Dev：题数、难度、数据库分布、库大小，以及 HF 版与完整包版题目的差异。

用法：uv run python scripts/data_stats.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main() -> None:
    hf = json.loads((DATA / "mini_dev_sqlite.json").read_text(encoding="utf-8"))
    zip_q = json.loads((DATA / "raw/zip/mini_dev_sqlite.json").read_text(encoding="utf-8"))

    print(f"HF 版题数：{len(hf)}；完整包版题数：{len(zip_q)}")
    print(f"question_id 唯一：{len({q['question_id'] for q in hf}) == len(hf)}")

    diff = Counter(q["difficulty"] for q in hf)
    print("\n难度分布：")
    for k in ("simple", "moderate", "challenging"):
        print(f"  {k:12s} {diff[k]:4d}  {diff[k] / len(hf):.1%}")

    no_evidence = sum(1 for q in hf if not q["evidence"].strip())
    print(f"\nevidence 为空的题：{no_evidence}")

    print("\n数据库（题数 / 表数 / 文件大小）：")
    by_db = Counter(q["db_id"] for q in hf)
    for db_id, n in sorted(by_db.items(), key=lambda x: -x[1]):
        path = DATA / "dev_databases" / db_id / f"{db_id}.sqlite"
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        conn.close()
        print(f"  {db_id:28s} {n:4d} 题  {tables:3d} 表  {path.stat().st_size / 2**20:8.1f} MB")

    # HF 版与完整包版逐题对比
    zip_by_id = {q["question_id"]: q for q in zip_q}
    changed = {"question": [], "evidence": [], "SQL": [], "difficulty": [], "db_id": []}
    missing = []
    for q in hf:
        z = zip_by_id.get(q["question_id"])
        if z is None:
            missing.append(q["question_id"])
            continue
        for field in changed:
            if q[field].strip() != z[field].strip():
                changed[field].append(q["question_id"])
    print("\nHF 版 vs 完整包版（按 question_id）：")
    print(f"  HF 有、包里没有：{len(missing)}")
    for field, ids in changed.items():
        print(f"  {field:10s} 不同：{len(ids):3d}  {ids[:10]}{' ...' if len(ids) > 10 else ''}")


if __name__ == "__main__":
    main()
