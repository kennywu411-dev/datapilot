"""提交门禁：冒烟集 = 分层抽样 20 题（seed=0，与试跑同一批）。

  uv run python evals/gate.py --offline        # 标准答案当模型跑全管线，必须 100%（不花钱）
  uv run python evals/gate.py --set-baseline   # 用 Haiku 跑一次，记为门禁基线（约 $0.006）
  uv run python evals/gate.py                  # Haiku 跑一次，EX 比基线下降超过阈值则退出码 1

由 .githooks/pre-commit 调用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datapilot import config  # noqa: E402
from evals.runner import Config, make_factory, run, stratified_sample  # noqa: E402

BASELINE = config.RUNS_DIR / "gate_baseline.json"
SMOKE_N = 20


def smoke_questions() -> list[dict]:
    qs = json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))
    return stratified_sample(qs, SMOKE_N, seed=0)


def offline_checks() -> list[str]:
    """不花钱的评测器回归检查，返回失败项。

    只跑"标准对标准"测不出顺序类 bug（两边顺序天然一致），所以同时检查：
    打乱行顺序必须全对；SELECT 1 命中的题必须与 gold-check 记录的完全一致。
    """
    from datapilot.ex import evaluate_one

    failures = []
    if run_smoke("gold", mock=True) != 1.0:
        failures.append("标准答案走全管线未达 100%")
    expected_select1 = set(SELECT1_HITS)
    for q in smoke_questions():
        path, gold = config.db_path(q["db_id"]), q["SQL"]
        shuffled = f"SELECT * FROM ({gold.strip().rstrip(';')}) ORDER BY RANDOM()"
        if not evaluate_one(path, shuffled, gold).correct:
            failures.append(f"q{q['question_id']} 打乱行顺序后判错")
        hit = evaluate_one(path, "SELECT 1", gold).correct
        if hit != (q["question_id"] in expected_select1):
            failures.append(f"q{q['question_id']} SELECT 1 判分与记录不一致")
    return failures


# gold-check-20261022-143358 中 SELECT 1 判对的题（与官方逐题一致）
SELECT1_HITS = (
    62,
    245,
    260,
    472,
    547,
    933,
    945,
    967,
    1134,
    1164,
    1171,
    1189,
    1275,
    1322,
    1352,
    1394,
)


def run_smoke(model: str, mock: bool) -> float:
    cfg = Config(model=model, pipeline="baseline", effort="medium", repeat=0)
    with tempfile.TemporaryDirectory() as tmp:
        rows = asyncio.run(
            run(smoke_questions(), [cfg], make_factory(mock), Path(tmp), 8, max_cost=0.5)
        )
    return sum(r["correct"] for r in rows) / SMOKE_N


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true")
    p.add_argument("--set-baseline", action="store_true")
    p.add_argument("--max-drop", type=float, default=0.10)
    args = p.parse_args()

    if args.offline:
        failures = offline_checks()
        for f in failures:
            print(f"[gate] ✗ {f}")
        print(f"[gate] 离线检查{'通过' if not failures else f'失败 {len(failures)} 项'}")
        return 1 if failures else 0

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    ex = run_smoke("haiku", mock=False)
    if args.set_baseline:
        BASELINE.write_text(json.dumps({"model": "haiku", "n": SMOKE_N, "ex": ex}, indent=2))
        print(f"[gate] 基线已记录：Haiku 冒烟 EX = {ex:.0%}")
        return 0
    base = json.loads(BASELINE.read_text())["ex"]
    print(f"[gate] Haiku 冒烟 EX = {ex:.0%}（基线 {base:.0%}，允许下降 {args.max_drop:.0%}）")
    return 0 if ex >= base - args.max_drop else 1


if __name__ == "__main__":
    sys.exit(main())
