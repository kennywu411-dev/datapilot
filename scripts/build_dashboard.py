"""从 runs/ 生成评测看板数据 web/src/data/eval.json（所有数字由运行记录现算，不手抄）。

用法：uv run python scripts/build_dashboard.py
"""

from __future__ import annotations

import argparse
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

# 运行 ID 集中在 runs/MANIFEST.json，重跑实验后只改那里；在 main() 里读，--help 不碰文件
MANIFEST: dict = {}
LADDER: list[tuple] = []
OPUS_E4 = E4_REP = QWEN = QWEN500 = CACHE = ""


def load_manifest() -> None:
    global MANIFEST, LADDER, OPUS_E4, E4_REP, QWEN, QWEN500, CACHE
    MANIFEST = json.loads((RUNS / "MANIFEST.json").read_text(encoding="utf-8"))
    LADDER = [tuple(x) for x in MANIFEST["ladder"]]
    OPUS_E4 = MANIFEST["opus_e4"]
    E4_REP = MANIFEST["e4_repeats"]
    QWEN = MANIFEST["local_model_100"]
    QWEN500 = MANIFEST["local_model_500"]
    CACHE = MANIFEST["cache_bench"]


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
    argparse.ArgumentParser(
        description=f"从 runs/MANIFEST.json 登记的运行记录生成看板数据 {OUT.relative_to(ROOT)}"
    ).parse_args()
    load_manifest()
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
    # 本地开源模型 500 题（1 次），与 Haiku E4 首次运行逐题配对
    # （compare 按 model 字段配对，先把本地模型的行改标成 haiku）
    q500 = rows(QWEN500)
    c = compare(e4, [{**x, "model": "haiku"} for x in q500], "haiku")
    final["qwen"] = {
        **stats(q500),
        "runs": None,
        "run_id": QWEN500,
        "paired_n": c["n"],
        "haiku_paired_ex": round(c["ex_a"], 4),
        "p_vs_haiku": float(f"{c['mcnemar_p']:.3g}"),
    }
    data["final"] = final

    # 国产 API 模型（v1.3）：500 题 E4 各 1 次，与 Haiku、Sonnet 的 E4 首次运行逐题配对
    cn = []
    for name, run in MANIFEST.get("cn_models", {}).items():
        r = rows(run)
        item = {"name": name, "run_id": run, **stats(r)}
        for base in ("haiku", "sonnet"):
            c = compare(e4, [{**x, "model": base} for x in r], base)
            item[f"p_vs_{base}"] = float(f"{c['mcnemar_p']:.3g}")
            item[f"{base}_paired_ex"] = round(c["ex_a"], 4)
        cn.append(item)
    data["cn_models"] = cn

    # 电商经营分析（模拟数据，v1.3）：有口径 vs 去掉口径，同 40 题
    shop = MANIFEST.get("shop_ops")
    if shop:
        with_ev = rows(shop["claude"]) + rows(shop["cn"])
        no_ev = rows(shop["claude_noev"]) + rows(shop["cn_noev"])
        names = {
            "haiku": "Haiku",
            "sonnet": "Sonnet",
            "deepseek": "deepseek-flash",
            "qwen": "qwen3.7-plus",
        }
        items = []
        for m, label in names.items():

            def runs_of(rs, m=m):
                reps = sorted({r["repeat"] for r in rs if r["model"] == m})
                return [[r for r in rs if r["model"] == m and r["repeat"] == k] for k in reps]

            w, n = runs_of(with_ev), runs_of(no_ev)
            ex = lambda rr: [round(sum(r["correct"] for r in x) / len(x), 4) for x in rr]  # noqa: E731
            items.append(
                {
                    "model": label,
                    "with_ev": ex(w),
                    "no_ev": ex(n),
                    "with_mean": round(statistics.mean(ex(w)), 4),
                    "no_mean": round(statistics.mean(ex(n)), 4),
                }
            )
        data["shop_ops"] = {"n": 40, "runs": shop, "models": items}

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

    # 智能路由（v1.3）：离线回放 3 条规则 + r1 实跑
    if "route_replay" in MANIFEST:
        rep = json.loads((RUNS / MANIFEST["route_replay"] / "summary.json").read_text())
        live = rows(MANIFEST["route_live"])
        esc = sum(
            1 for r in live if any(t.get("step") == "escalate" for t in (r.get("trace") or []))
        )
        data["route"] = {
            "replay_run": MANIFEST["route_replay"],
            "live_run": MANIFEST["route_live"],
            "haiku": rep["baselines"]["haiku"],
            "sonnet": rep["baselines"]["sonnet"],
            "oracle": rep["oracle"],
            "rules": {
                k: {kk: v[kk] for kk in ("ex_mean", "ex_std", "escalated_mean", "cost_mean")}
                for k, v in rep["rules"].items()
            },
            "live": {
                "ex": sum(r["correct"] for r in live) / len(live),
                "escalated": esc,
                "cost": round(sum(r["cost_usd"] for r in live), 4),
            },
        }

    # 中文问题（v1.3 D2）：BIRD 同批 100 题，中英文配对
    if "zh100" in MANIFEST:
        z, e = rows(MANIFEST["zh100"]["zh"]), rows(MANIFEST["zh100"]["en"])
        zh = {}
        for m in ("haiku", "sonnet"):
            c = compare(e, z, m)
            zh[m] = {
                "en": round(c["ex_a"], 4),
                "zh": round(c["ex_b"], 4),
                "en_only": len(c["a_only_correct"]),
                "zh_only": len(c["b_only_correct"]),
                "p": round(c["mcnemar_p"], 3),
            }
        data["zh100"] = {"runs": MANIFEST["zh100"], "models": zh}

    data["cache"] = json.loads((RUNS / CACHE / "summary.json").read_text())
    data["selfcheck"] = json.loads((RUNS / MANIFEST["gold_check"] / "summary.json").read_text())

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
        "qwen 500:",
        final["qwen"]["ex"],
        "p vs Haiku:",
        final["qwen"]["p_vs_haiku"],
    )


if __name__ == "__main__":
    main()
