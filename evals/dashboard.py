"""评测静态看板（evalkit）：把 runs/ 下的运行汇总成一个 HTML 文件，直接用浏览器打开。

  uv run python evals/dashboard.py                       # MANIFEST 登记的全部运行
  uv run python evals/dashboard.py <运行 ID> ... -o site/eval.html

实验列表、配对检验（浏览器里现算）、失败题回放（SQL 和中间步骤）、成本与延迟。
接入 evalkit 之前的运行在这里现场转换，原文件不改。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evalkit import store  # noqa: E402
from evalkit.dashboard import build  # noqa: E402

from evals.report import evalkit_rows  # noqa: E402

RUNS = ROOT / "runs"


def manifest_runs() -> list[str]:
    found: list[str] = []

    def walk(x):
        if isinstance(x, str) and (RUNS / x / "results.jsonl").exists():
            found.append(x)
        elif isinstance(x, list | tuple):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            for y in x.values():
                walk(y)

    walk(json.loads((RUNS / "MANIFEST.json").read_text(encoding="utf-8")))
    return list(dict.fromkeys(found))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("runs", nargs="*", help="runs/ 下的运行目录名，默认 MANIFEST 登记的全部")
    p.add_argument("-o", "--output", default=str(ROOT / "site" / "eval.html"))
    args = p.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="datapilot-dashboard-")) / "datapilot"
    dirs = []
    for name in args.runs or manifest_runs():
        try:
            rows = evalkit_rows(RUNS / name)
        except (KeyError, TypeError):  # 缓存测速、权限评测等不是逐题判分的运行
            continue
        d = tmp / name
        store.write_rows(d, rows)
        questions = store.load_config(RUNS / name).get("questions") or "bird-minidev"
        store.write_json(d / store.CONFIG, {"dataset": {"name": Path(questions).stem}})
        dirs.append(d)
    out = build(dirs, Path(args.output), title="DataPilot 评测看板")
    print(f"{len(dirs)} 次运行 → {out}")


if __name__ == "__main__":
    main()
