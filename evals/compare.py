"""两次运行的配对比较：同一批题、同一模型，A → B 的 EX 变化是否显著。

  uv run python evals/compare.py <run_a> <run_b> --model sonnet
  uv run python evals/compare.py <run_a> <run_b> --model haiku --repeat 2 --slice difficulty

不加 --repeat 时同一题以最后一条结果为准（与历史口径一致）；加 --repeat 时用 evalkit 按指定的
那一次重复配对，适合各跑了多次的运行（如语义层 3 次对 3 次），可按难度等子集拆分。

- McNemar 精确检验：只看"A 对 B 错"与"B 对 A 错"的题数，比较两个总分更严谨
- 纠错统计：B 中纠错改动了 SQL 的题，改对 / 改错 / 仍错 / 仍对 的数量
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evalkit.compare import compare as ek_compare  # noqa: E402
from evalkit.compare import render as ek_render  # noqa: E402
from evalkit.stats import mcnemar_exact  # noqa: E402,F401  （双侧精确检验，其他脚本从这里导入）

from evals.report import evalkit_rows, load_rows  # noqa: E402


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
    ap.add_argument("--repeat", type=int, help="按第几次重复配对（从 0 开始），用 evalkit 比较")
    ap.add_argument("--slice", action="append", help="按子集拆分（如 difficulty），配合 --repeat")
    args = ap.parse_args()
    if args.repeat is not None:
        c = ek_compare(
            evalkit_rows(ROOT / "runs" / args.run_a),
            evalkit_rows(ROOT / "runs" / args.run_b),
            args.model,
            args.model,
            args.repeat,
            args.slice or (),
        )
        print(ek_render(c))
        return
    out = compare(
        load_rows(ROOT / "runs" / args.run_a), load_rows(ROOT / "runs" / args.run_b), args.model
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
