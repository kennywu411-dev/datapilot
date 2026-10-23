"""智能路由离线回放（不花钱）：用已有的 E4 运行模拟三条升级规则。

E4 首次运行和两次重复里，Haiku 和 Sonnet 都对 500 题各跑过一遍（互相独立的真实调用）。
对每道题，规则看 Haiku 这次的运行信号（SQL、调用次数、轨迹），决定用 Haiku 的结果，
还是"Haiku 先跑 + Sonnet 重跑"的结果。成本 = Haiku 成本 + 升级时的 Sonnet 成本；
延迟 = 两者相加（串行）。这和实跑的路由流程（RoutedPipeline）逻辑一致，
因为实跑里 Sonnet 也是从头独立重跑。

结果写到 runs/route-replay-<时间>/，运行 ID 用于登记 numbers.md。
用法：uv run python scripts/route_replay.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from datapilot.routing import RULES  # noqa: E402
from evals.compare import mcnemar_exact  # noqa: E402
from evals.report import load_rows, pct  # noqa: E402

MANIFEST = json.loads((ROOT / "runs" / "MANIFEST.json").read_text(encoding="utf-8"))
E4 = MANIFEST["ladder"][4][2]
REP = MANIFEST["e4_repeats"]


def replicates() -> list[tuple[str, dict, dict]]:
    """三组 (标签, Haiku 按题, Sonnet 按题)。"""
    e4, rep = load_rows(ROOT / "runs" / E4), load_rows(ROOT / "runs" / REP)
    out = [("e4", e4, 0)] + [("rep", rep, k) for k in (0, 1)]
    groups = []
    for name, rows, k in out:
        by = {
            m: {r["question_id"]: r for r in rows if r["model"] == m and r["repeat"] == k}
            for m in ("haiku", "sonnet")
        }
        groups.append((f"{name}-r{k}", by["haiku"], by["sonnet"]))
    return groups


def paired_p(a: dict, b: dict) -> float:
    a_only = sum(1 for q in a if a[q] and not b[q])
    b_only = sum(1 for q in a if b[q] and not a[q])
    return mcnemar_exact(a_only, b_only)


def main() -> None:
    run_id = f"route-replay-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = ROOT / "runs" / run_id
    out_dir.mkdir(parents=True)
    groups = replicates()
    summary, lines = {"source_runs": [E4, REP], "rules": {}, "baselines": {}}, []

    for m in ("haiku", "sonnet"):
        exs = [
            sum(r["correct"] for r in (h if m == "haiku" else s).values()) / len(h)
            for _, h, s in groups
        ]
        costs = [
            sum(r["cost_usd"] for r in (h if m == "haiku" else s).values()) for _, h, s in groups
        ]
        summary["baselines"][m] = {
            "ex_runs": exs,
            "ex_mean": statistics.mean(exs),
            "cost_mean": statistics.mean(costs),
        }

    for rule, fn in RULES.items():
        per = []
        for label, h, s in groups:
            choice, correct, cost, lat = {}, {}, 0.0, []
            for q, hr in h.items():
                esc = fn(hr.get("pred_sql") or "", hr.get("llm_calls") or 0, hr.get("trace") or [])
                r = s[q] if esc else hr
                choice[q], correct[q] = esc, r["correct"]
                cost += hr["cost_usd"] + (s[q]["cost_usd"] if esc else 0)
                lat.append(hr["latency_s"] + (s[q]["latency_s"] if esc else 0))
                lines.append(
                    {
                        "replicate": label,
                        "rule": rule,
                        "question_id": q,
                        "escalated": esc,
                        "correct": r["correct"],
                    }
                )
            hc = {q: r["correct"] for q, r in h.items()}
            sc = {q: r["correct"] for q, r in s.items()}
            lat.sort()
            per.append(
                {
                    "replicate": label,
                    "ex": sum(correct.values()) / len(correct),
                    "escalated": sum(choice.values()),
                    "cost": cost,
                    "p50": pct(lat, 0.5),
                    "p95": pct(lat, 0.95),
                    "p_vs_haiku": paired_p(correct, hc),
                    "p_vs_sonnet": paired_p(correct, sc),
                    "haiku_cost": sum(r["cost_usd"] for r in h.values()),
                    "sonnet_cost": sum(r["cost_usd"] for r in s.values()),
                }
            )
        exs = [p["ex"] for p in per]
        summary["rules"][rule] = {
            "replicates": per,
            "ex_mean": statistics.mean(exs),
            "ex_std": statistics.stdev(exs),
            "escalated_mean": statistics.mean(p["escalated"] for p in per),
            "cost_mean": statistics.mean(p["cost"] for p in per),
            "cost_ratio_vs_sonnet": statistics.mean(p["cost"] / p["sonnet_cost"] for p in per),
        }

    # 参照上限：假设能预知 Haiku 哪些题会错，只把这些题交给 Sonnet（现实中做不到）
    oracle = []
    for _, h, s in groups:
        wrong = [q for q in h if not h[q]["correct"]]
        oracle.append(
            {
                "ex": sum(h[q]["correct"] or s[q]["correct"] for q in h) / len(h),
                "escalated": len(wrong),
                "cost": sum(r["cost_usd"] for r in h.values())
                + sum(s[q]["cost_usd"] for q in wrong),
            }
        )
    summary["oracle"] = {
        "ex_mean": statistics.mean(o["ex"] for o in oracle),
        "escalated_mean": statistics.mean(o["escalated"] for o in oracle),
        "cost_mean": statistics.mean(o["cost"] for o in oracle),
    }

    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")

    b = summary["baselines"]
    print(f"run_id={run_id}（来源 {E4}、{REP}，3 组配对）\n")
    print(
        "| 方案 | EX（3 次均值） | 升级题数 | 500 题成本 | 成本 / 全 Sonnet "
        "| 各次 p vs Haiku | 各次 p vs Sonnet |"
    )
    print("|---|---|---|---|---|---|---|")
    print(
        f"| 全用 Haiku | {b['haiku']['ex_mean']:.1%} | 0 | ${b['haiku']['cost_mean']:.3f} | "
        f"{b['haiku']['cost_mean'] / b['sonnet']['cost_mean']:.0%} | — | — |"
    )
    print(
        f"| 全用 Sonnet | {b['sonnet']['ex_mean']:.1%} | 500 | "
        f"${b['sonnet']['cost_mean']:.3f} | 100% | — | — |"
    )
    o = summary["oracle"]
    print(
        f"| 理想路由（参照上限） | {o['ex_mean']:.1%} | {o['escalated_mean']:.0f} | "
        f"${o['cost_mean']:.3f} | "
        f"{o['cost_mean'] / b['sonnet']['cost_mean']:.0%} | — | — |"
    )
    for rule, r in summary["rules"].items():
        ph = " / ".join(f"{p['p_vs_haiku']:.3f}" for p in r["replicates"])
        ps = " / ".join(f"{p['p_vs_sonnet']:.3f}" for p in r["replicates"])
        print(
            f"| {rule} | {r['ex_mean']:.1%} ± {r['ex_std'] * 100:.2f} | "
            f"{r['escalated_mean']:.0f} | "
            f"${r['cost_mean']:.3f} | {r['cost_ratio_vs_sonnet']:.0%} | {ph} | {ps} |"
        )


if __name__ == "__main__":
    main()
