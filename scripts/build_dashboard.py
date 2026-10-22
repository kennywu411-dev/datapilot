"""从 runs/ 生成评测看板数据 web/src/data/eval.json（所有数字由运行记录现算，不手抄）。

用法：uv run python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.compare import compare  # noqa: E402
from evals.report import load_rows, pct, wilson  # noqa: E402

RUNS = ROOT / "runs"
OUT = ROOT / "web" / "src" / "data" / "eval.json"

LADDER = [
    ("E0", "基线（官方提示词）", "baseline-20261022-144835-8314"),
    ("E1", "提示缓存", "e1-20261022-161608-d69f"),
    ("E2", "+ Schema 增强", "e2-20261022-162100-555b"),
    ("E3", "+ 值核对", "e3-20261022-162602-47f5"),
    ("E4", "+ 执行纠错（最终）", "e4-20261022-163051-724f"),
    ("Agent", "自主调用工具", "agent-20261022-163640-2775"),
]
OPUS_E4 = "e4opus-20261022-175535-704b"
E4_REP = "e4rep-20261022-174522-183a"
QWEN = "qwen100-20261022-175906-933f"
CACHE = "cache-bench-20261022-183428"


def rows(run: str) -> list[dict]:
    return load_rows(RUNS / run)


def stats(rs: list[dict]) -> dict:
    n = len(rs)
    k = sum(r["correct"] for r in rs)
    lat = sorted(r["latency_s"] for r in rs)
    lo, hi = wilson(k, n)
    return {
        "n": n,
        "ex": round(k / n, 4),
        "ci": [round(lo, 4), round(hi, 4)],
        "cost_per_q": round(sum(r["cost_usd"] for r in rs) / n, 6),
        "p50": round(pct(lat, 0.5), 2),
        "p95": round(pct(lat, 0.95), 2),
        "by_difficulty": {
            d: round(
                sum(r["correct"] for r in rs if r["difficulty"] == d)
                / max(1, sum(1 for r in rs if r["difficulty"] == d)),
                4,
            )
            for d in ("simple", "moderate", "challenging")
        },
    }


def main() -> None:
    data: dict = {"generated_from": "runs/（由 scripts/build_dashboard.py 生成）"}

    # 逐项实验阶梯（Sonnet 主、Haiku 验证）+ 相对上一步的配对检验
    ladder = []
    prev = None
    for key, label, run in LADDER:
        r = rows(run)
        item = {"key": key, "label": label, "run_id": run}
        for m in ("sonnet", "haiku"):
            rm = [x for x in r if x["model"] == m]
            item[m] = stats(rm)
            if prev and key != "Agent":
                c = compare(rows(prev), r, m)
                item[m]["p_vs_prev"] = round(c["mcnemar_p"], 4)
        if key == "Agent":
            c = {m: compare(rows(LADDER[4][2]), r, m) for m in ("sonnet", "haiku")}
            for m in c:
                item[m]["p_vs_e4"] = round(c[m]["mcnemar_p"], 4)
        ladder.append(item)
        prev = run
    base_h = compare(rows(LADDER[0][2]), rows(LADDER[4][2]), "haiku")
    data["ladder"] = ladder
    data["haiku_e0_to_e4_p"] = round(base_h["mcnemar_p"], 4)

    # 最终配置：三档 500 题 + 稳定性
    e4 = rows(LADDER[4][2])
    rep = rows(E4_REP)
    final = {}
    for m in ("haiku", "sonnet"):
        runs3 = [[x for x in e4 if x["model"] == m]] + [
            [x for x in rep if x["model"] == m and x["repeat"] == k] for k in (0, 1)
        ]
        exs = [sum(x["correct"] for x in rs) / len(rs) for rs in runs3]
        final[m] = {
            **stats(runs3[0]),
            "runs": [round(e, 4) for e in exs],
            "mean": round(statistics.mean(exs), 4),
            "std": round(statistics.stdev(exs), 4),
        }
    final["opus"] = {**stats(rows(OPUS_E4)), "runs": None, "contamination_note": True}
    data["final"] = final

    # 同 100 题：本地开源模型 vs Claude
    q = rows(QWEN)
    ids = {x["question_id"] for x in q}
    same100 = {"qwen2.5-coder:7b": stats(q)}
    for m, run in (("haiku", LADDER[4][2]), ("sonnet", LADDER[4][2]), ("opus", OPUS_E4)):
        same100[m] = stats([x for x in rows(run) if x["model"] == m and x["question_id"] in ids])
    data["same100"] = same100

    # 失败分析：50 道人工判定
    sample = json.loads((ROOT / "docs/failure/sample50.json").read_text())
    judg = json.loads((ROOT / "docs/failure/judgments.json").read_text())
    names = {
        "A": "模型错",
        "B": "标准答案错",
        "C": "题目歧义",
        "D": "评测缺陷",
        "E": "两者都错",
        "F": "系统故障",
    }
    counts = {k: sum(1 for v in judg.values() if v["final"] == k) for k in names}
    data["failure"] = {
        "source_run": sample["source_run"],
        "n_failures": sample["n_failures"],
        "names": names,
        "counts": counts,
        "cases": [
            {
                "qid": s["qid"],
                "db": s["db"],
                "difficulty": s["difficulty"],
                "question": s["question"],
                "evidence": s["evidence"],
                "gold_sql": s["gold_sql"],
                "pred_sql": s["pred_sql"],
                "gold": s["gold"],
                "pred": s["pred"],
                "category": judg[str(s["qid"])]["final"],
                "reason": judg[str(s["qid"])]["reason"],
            }
            for s in sample["sample"]
        ],
    }

    data["cache"] = json.loads((RUNS / CACHE / "summary.json").read_text())
    data["selfcheck"] = json.loads(
        (RUNS / "gold-check-20261022-145326" / "summary.json").read_text()
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"写入 {OUT.relative_to(ROOT)}（{OUT.stat().st_size // 1024} KB）")
    print(
        "Haiku E4:",
        final["haiku"]["mean"],
        "Sonnet E4:",
        final["sonnet"]["mean"],
        "Opus:",
        final["opus"]["ex"],
    )


if __name__ == "__main__":
    main()
