"""两次运行的配对比较：同一批题、同一模型，A → B 的 EX 变化是否显著。

  uv run python evals/compare.py <run_a> <run_b> --model sonnet

- McNemar 精确检验：只看"A 对 B 错"与"B 对 A 错"的题数，比较两个总分更严谨
- 纠错统计：B 中纠错改动了 SQL 的题，改对 / 改错 / 仍错 / 仍对 的数量
"""

from __future__ import annotations

import argparse
import json
import sys
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.report import load_rows  # noqa: E402


def mcnemar_exact(b: int, c: int) -> float:
    """双侧精确 McNemar：b = A 对 B 错，c = B 对 A 错。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * p)


def compare(rows_a: list[dict], rows_b: list[dict], model: str) -> dict:
    a = {r["question_id"]: r for r in rows_a if r["model"] == model}
    b = {r["question_id"]: r for r in rows_b if r["model"] == model}
    ids = sorted(set(a) & set(b))
    only_a = [i for i in ids if a[i]["correct"] and not b[i]["correct"]]
    only_b = [i for i in ids if b[i]["correct"] and not a[i]["correct"]]
    n = len(ids)
    corrected = [
        b[i] for i in ids if b[i].get("first_sql") and b[i]["first_sql"] != b[i]["pred_sql"]
    ]
    return {
        "model": model,
        "n": n,
        "ex_a": sum(a[i]["correct"] for i in ids) / n,
        "ex_b": sum(b[i]["correct"] for i in ids) / n,
        "a_only_correct": only_a,
        "b_only_correct": only_b,
        "mcnemar_p": mcnemar_exact(len(only_a), len(only_b)),
        "cost_a": sum(a[i]["cost_usd"] for i in ids),
        "cost_b": sum(b[i]["cost_usd"] for i in ids),
        "llm_calls_b_mean": sum(b[i].get("llm_calls", 1) for i in ids) / n,
        "corrections": {
            "changed_sql": len(corrected),
            "fixed": sum(1 for r in corrected if r["correct"] and not r["first_correct"]),
            "broke": sum(1 for r in corrected if not r["correct"] and r["first_correct"]),
            "still_wrong": sum(1 for r in corrected if not r["correct"] and not r["first_correct"]),
            "still_right": sum(1 for r in corrected if r["correct"] and r["first_correct"]),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_a")
    ap.add_argument("run_b")
    ap.add_argument("--model", default="sonnet")
    args = ap.parse_args()
    out = compare(
        load_rows(ROOT / "runs" / args.run_a), load_rows(ROOT / "runs" / args.run_b), args.model
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
