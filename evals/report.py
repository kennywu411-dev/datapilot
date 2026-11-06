"""评测报告：EX + Wilson 95% 置信区间、按难度拆分、成本、延迟、失败状态。

用法：uv run python evals/report.py --run <run_id>
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from evalkit import stats as ek_stats

ROOT = Path(__file__).resolve().parents[1]
GROUP_KEYS = ("model", "pipeline", "effort")


# 区间和分位数的口径由评测平台 evalkit 统一提供（与本项目原实现逐位一致）
wilson = ek_stats.wilson
pct = ek_stats.percentile  # 排序后取第 int(q·n) 个，不插值；调用方传入的列表已排好序也无妨


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in GROUP_KEYS)].append(r)
    out = []
    for key, rs in sorted(groups.items()):
        n = len(rs)
        k = sum(r["correct"] for r in rs)
        lat = sorted(r["latency_s"] for r in rs)
        by_diff = {}
        for d in ("simple", "moderate", "challenging"):
            sub = [r for r in rs if r["difficulty"] == d]
            if sub:
                by_diff[d] = (sum(r["correct"] for r in sub), len(sub))
        out.append(
            {
                **dict(zip(GROUP_KEYS, key, strict=True)),
                "n": n,
                "repeats": len({r["repeat"] for r in rs}),
                "ex": k / n,
                "ci95": wilson(k, n),
                "by_difficulty": by_diff,
                "status": dict(Counter(r["status"] for r in rs)),
                "cost_total": sum(r["cost_usd"] for r in rs),
                "cost_per_q": sum(r["cost_usd"] for r in rs) / n,
                "latency_p50": pct(lat, 0.5),
                "latency_p95": pct(lat, 0.95),
            }
        )
    return out


def render(summary: list[dict]) -> str:
    lines = [
        "| 模型 | 流程 | effort | 题次 | EX | 95% CI | simple | moderate | challenging "
        "| 单题成本 | 总成本 | P50 / P95 延迟 | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        diff = []
        for d in ("simple", "moderate", "challenging"):
            kn = s["by_difficulty"].get(d)
            diff.append(f"{kn[0]}/{kn[1]}" if kn else "-")
        lo, hi = s["ci95"]
        lines.append(
            f"| {s['model']} | {s['pipeline']} | {s['effort']} | {s['n']} | {s['ex']:.1%} "
            f"| {lo:.1%}–{hi:.1%} | {' | '.join(diff)} | ${s['cost_per_q']:.4f} "
            f"| ${s['cost_total']:.3f} | {s['latency_p50']:.1f}s / {s['latency_p95']:.1f}s "
            f"| {s['status']} |"
        )
    return "\n".join(lines)


def load_rows(run_dir: Path) -> list[dict]:
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    # 续跑时出错的题会被重跑，同一 key 以最后一条为准
    latest = {
        (r["question_id"], r["model"], r["pipeline"], r["effort"], r["repeat"]): r for r in rows
    }
    return list(latest.values())


def evalkit_rows(run_dir: Path) -> list[dict]:
    """任意一次运行的 evalkit 结果行：新运行本身就带 evalkit 字段，旧运行现场补上。"""
    out = []
    for r in load_rows(run_dir):
        if "item_id" not in r:
            r = r | {
                "item_id": r["question_id"],
                "variant": r["model"],
                "passed": bool(r["correct"]),
                "tags": {"difficulty": r.get("difficulty"), "db_id": r.get("db_id")},
                "scores": {
                    "execution_match": {"value": float(r["correct"]), "passed": r["correct"]}
                },
                "output": r.get("pred_sql"),
            }
        out.append(r)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = ROOT / "runs" / args.run
    summary = summarize(load_rows(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(render(summary))


if __name__ == "__main__":
    main()
