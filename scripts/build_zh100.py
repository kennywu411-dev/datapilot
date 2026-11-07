"""D2：从校对表生成中文题集 evals/zh100/zh100.json（与 BIRD 原题同 question_id，可直接配对比较）。

校对表 evals/zh100/校对表.csv 的"校对后"一列留空表示采用初译稿；填了就用填的。
题集只替换 question（原文保存在 question_en），evidence、SQL、db_id 等保持 BIRD 原样。

用法：uv run python scripts/build_zh100.py
跑评测：uv run python evals/runner.py --models sonnet --pipeline e4_correct \\
        --questions evals/zh100/zh100.json --tag zh100
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datapilot import config  # noqa: E402

DIR = ROOT / "evals" / "zh100"
SHEET = DIR / "校对表.csv"
OUT = DIR / "zh100.json"


def main() -> None:
    bird = {q["question_id"]: q for q in json.loads(config.QUESTIONS_PATH.read_text("utf-8"))}
    with SHEET.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out, reviewed = [], 0
    for r in rows:
        q = dict(bird[int(r["question_id"])])
        final = r["校对后（留空=采用草稿）"].strip()
        reviewed += bool(final)
        q["question_en"] = q["question"]
        q["question"] = final or r["中文草稿"].strip()
        q["needs_mapping"] = r["取值映射"].strip() == "是"
        out.append(q)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}：{len(out)} 题（{reviewed} 题有校对改动）")


if __name__ == "__main__":
    main()
