"""模型对比（v1.3）：新模型的 E4 全量 vs Claude 三档，含配对检验和污染信号。

- 配对检验：与 Haiku、Sonnet 的 E4 首次运行逐题做 McNemar（同题集）
- 语法 / 执行错误率：预测 SQL 执行报错（status = pred_error）的比例
- 污染信号（问题日志 P7 的方法）：预测与标准 SQL 归一化后逐字相同的题数；
  标准答案用 T1 别名的题里，模型也用 T1 的题数。所有模型统一用 E4 结果、同一套归一化

用法：uv run python scripts/model_compare.py deepseek=<run_id> qwen=<run_id>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from datapilot import config  # noqa: E402
from evals.compare import compare  # noqa: E402
from evals.report import load_rows, pct, wilson  # noqa: E402

MANIFEST = json.loads((ROOT / "runs" / "MANIFEST.json").read_text(encoding="utf-8"))
E4, OPUS = MANIFEST["ladder"][4][2], MANIFEST["opus_e4"]
T1 = re.compile(r"\bT1\b")


def norm(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).strip().lower()


def stats(rows: list[dict], gold: dict) -> dict:
    n, k = len(rows), sum(r["correct"] for r in rows)
    lo, hi = wilson(k, n)
    lat = sorted(r["latency_s"] for r in rows)
    t1_gold = [r for r in rows if T1.search(gold[r["question_id"]])]
    return {
        "n": n,
        "ex": k / n,
        "ci": [lo, hi],
        "by_difficulty": {
            d: sum(r["correct"] for r in rows if r["difficulty"] == d)
            / max(1, sum(1 for r in rows if r["difficulty"] == d))
            for d in ("simple", "moderate", "challenging")
        },
        "cost_per_q": sum(r["cost_usd"] for r in rows) / n,
        "p50": pct(lat, 0.5),
        "p95": pct(lat, 0.95),
        "pred_error": sum(r["status"] == "pred_error" for r in rows),
        "status": {
            s: sum(r["status"] == s for r in rows) for s in sorted({r["status"] for r in rows})
        },
        "verbatim": sum(norm(r["pred_sql"]) == norm(gold[r["question_id"]]) for r in rows),
        "t1_gold": len(t1_gold),
        "t1_both": sum(bool(T1.search(r["pred_sql"] or "")) for r in t1_gold),
    }


def main(argv: list[str]) -> None:
    gold = {
        q["question_id"]: q["SQL"] for q in json.loads(config.QUESTIONS_PATH.read_text("utf-8"))
    }
    e4 = load_rows(ROOT / "runs" / E4)
    ref = {
        "haiku": [r for r in e4 if r["model"] == "haiku"],
        "sonnet": [r for r in e4 if r["model"] == "sonnet"],
        "opus": [r for r in load_rows(ROOT / "runs" / OPUS) if r["model"] == "opus"],
    }
    out = {"reference_runs": {"haiku": E4, "sonnet": E4, "opus": OPUS}, "models": {}}
    for m, rows in ref.items():
        out["models"][m] = stats(rows, gold)
    for arg in argv:
        name, run = arg.split("=", 1)
        rows = load_rows(ROOT / "runs" / run)
        s = stats(rows, gold)
        s["run_id"] = run
        for base in ("haiku", "sonnet"):
            c = compare(ref[base], [{**r, "model": base} for r in rows], base)
            s[f"vs_{base}"] = {
                "n": c["n"],
                "base_only": len(c["a_only_correct"]),
                "new_only": len(c["b_only_correct"]),
                "p": c["mcnemar_p"],
            }
        out["models"][name] = s

    print(
        "| 模型 | EX | 95% CI | simple / moderate / challenging | 单题成本 | P50 / P95 "
        "| 执行报错 | 逐字复现 | T1 同用 |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for m, s in out["models"].items():
        bd = " / ".join(f"{v:.0%}" for v in s["by_difficulty"].values())
        print(
            f"| {m} | {s['ex']:.1%} | {s['ci'][0]:.1%}–{s['ci'][1]:.1%} | {bd} | "
            f"${s['cost_per_q']:.5f} | "
            f"{s['p50']:.1f}s / {s['p95']:.1f}s | {s['pred_error']} | {s['verbatim']} | "
            f"{s['t1_both']}/{s['t1_gold']} |"
        )
    for m, s in out["models"].items():
        for base in ("haiku", "sonnet"):
            if f"vs_{base}" in s:
                v = s[f"vs_{base}"]
                print(
                    f"{m} vs {base}：{base} 独对 {v['base_only']}，{m} 独对 {v['new_only']}，"
                    f"McNemar p={v['p']:.3g}"
                )
    path = ROOT / "runs" / "model-compare-v1.3.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n写入 {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
