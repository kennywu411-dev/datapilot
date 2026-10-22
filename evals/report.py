"""评测报告：EX + Wilson 95% 置信区间、按难度拆分、成本、延迟、失败状态。

用法：uv run python evals/report.py --run <run_id>
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUP_KEYS = ("model", "pipeline", "effort")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


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
