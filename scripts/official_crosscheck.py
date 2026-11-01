"""用 BIRD 官方 evaluation_ex.py 的判分函数逐题判分，与 datapilot.ex 对照。

官方脚本只输出汇总分，这里直接调用它的 execute_model（含 func_timeout 与 set 比较），
拿到逐题结果。三组输入：gold 自身、打乱行顺序、恒 SELECT 1。

官方依赖 psycopg2 / pymysql（只为 import），用临时环境运行：
uv run --with func-timeout --with psycopg2-binary --with pymysql \
    python scripts/official_crosscheck.py runs/<gold-check-run>/
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party" / "bird_eval"))

import evaluation_ex as official  # noqa: E402

DB_ROOT = ROOT / "data" / "dev_databases"


def shuffled(gold: str) -> str:
    return f"SELECT * FROM ({gold.strip().rstrip(';')}) ORDER BY RANDOM()"


def judge(args: tuple) -> tuple[int, str, int]:
    qid, kind, pred, gold, db = args
    out = official.execute_model(pred, gold, db, qid, 30.0, "SQLite")
    return qid, kind, out["res"]


def main() -> None:
    run_dir = Path(sys.argv[1])
    ours = {
        json.loads(line)["question_id"]: json.loads(line)
        for line in (run_dir / "gold_check.jsonl").read_text().splitlines()
    }
    questions = json.loads((ROOT / "data" / "mini_dev_sqlite.json").read_text())
    jobs = []
    for q in questions:
        db = str(DB_ROOT / q["db_id"] / f"{q['db_id']}.sqlite")
        preds = (("gold", q["SQL"]), ("shuffle", shuffled(q["SQL"])), ("select1", "SELECT 1"))
        for kind, pred in preds:
            jobs.append((q["question_id"], kind, pred, q["SQL"], db))

    with mp.Pool(8) as pool:
        results = pool.map(judge, jobs, chunksize=4)

    official_res: dict[str, dict[int, int]] = {"gold": {}, "shuffle": {}, "select1": {}}
    for qid, kind, res in results:
        official_res[kind][qid] = res

    n = len(questions)
    report = {"source_run": run_dir.name}
    checks = (("gold", "gold_self_ok"), ("shuffle", "shuffle_ok"), ("select1", "select1_ok"))
    for kind, our_key in checks:
        report[f"official_{kind}_acc"] = sum(official_res[kind].values()) / n
        report[f"official_{kind}_fail" if kind != "select1" else "official_select1_hit"] = sorted(
            qid for qid, r in official_res[kind].items() if (r == 0) != (kind == "select1")
        )
        report[f"mismatch_{kind}"] = sorted(
            qid for qid in ours if bool(ours[qid][our_key]) != bool(official_res[kind][qid])
        )
    (run_dir / "official_crosscheck.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
