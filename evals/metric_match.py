"""指标匹配准确率（离线，不调用模型）：语义层匹配到的指标 / 术语 vs 人工标注。

  uv run python evals/metric_match.py evals/shop_ops/questions_noev.json \
      evals/shop_ops/metric_labels.json

按 (题, 指标) 计精确率、召回率，另报整题完全一致的比例，并列出漏配和误配的题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datapilot.semantic import load_layer  # noqa: E402


def score(questions: list[dict], labels: dict[str, list[str]]) -> dict:
    tp = fp = fn = exact = 0
    cases = []
    for q in questions:
        layer = load_layer(q["db_id"])
        if layer is None:
            raise SystemExit(f"{q['db_id']} 没有指标字典，不能评测指标匹配")
        matches = layer.match(q["question"])
        got = {m.id for m in matches}
        want = set(labels[str(q["question_id"])])
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        exact += got == want
        if got != want:
            cases.append(
                {
                    "question_id": q["question_id"],
                    "question": q["question"],
                    "missed": sorted(want - got),
                    "spurious": sorted(got - want),
                    "via": {m.id: f"{m.via}:{m.text}" for m in matches},
                }
            )
    return {
        "n": len(questions),
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "exact": exact / len(questions),
        "pairs": {"tp": tp, "fp": fp, "fn": fn},
        "cases": cases,
    }


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("questions")
    p.add_argument("labels")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args(argv)
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    out = score(questions, labels)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    print(
        f"{out['n']} 题：精确率 {out['precision']:.1%}，召回率 {out['recall']:.1%}，"
        f"整题一致 {out['exact']:.1%}（{out['pairs']}）"
    )
    for c in out["cases"]:
        missed, spurious = c["missed"], c["spurious"]
        print(f"q{c['question_id']} 漏 {missed} 误 {spurious} | {c['question']} | {c['via']}")


if __name__ == "__main__":
    main()
