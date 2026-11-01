"""下载 BIRD Mini-Dev（CC BY-SA 4.0）。

- 题目与标准 SQL：Hugging Face `birdsql/bird_mini_dev`（官方 README 称其为 canonical 版本），
  固定取 2026-01-18 的修订 f65faf4：main 分支后来换成了另一版题库（题号、题目都不同，
  文件名也改为 mini_dev_sqlite.jsonl），而本仓库所有运行记录和门禁对应的是 f65faf4 这一版
- SQLite 数据库：官方完整包 minidev.zip（约 801MB），只解出 SQLite 数据库

用法：uv run python scripts/download_data.py
国内访问 HF 慢时：HF_ENDPOINT=https://hf-mirror.com uv run python scripts/download_data.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
DB_DIR = DATA / "dev_databases"
QUESTIONS = DATA / "mini_dev_sqlite.json"

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
HF_REVISION = "f65faf4ae3b638c1fa6df1d3370c8d92c8366301"  # 2026-01-18；运行记录用的就是这一版
HF_REPO = f"{HF_ENDPOINT}/datasets/birdsql/bird_mini_dev/resolve/{HF_REVISION}"
HF_FILE = f"{HF_REPO}/data/mini_dev_sqlite-00000-of-00001.json"
ZIP_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
ZIP_PATH = RAW / "minidev.zip"


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"已存在，跳过：{dest.relative_to(ROOT)}")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if done % (50 << 20) < (1 << 20):
                if total:
                    print(
                        f"  {done / total:6.1%}  {done / 2**20:.1f} / {total / 2**20:.1f} MB",
                        flush=True,
                    )
                else:
                    print(f"  已下载 {done / 2**20:.1f} MB（服务器未给出总大小）", flush=True)
    tmp.rename(dest)


def load_questions(path: Path) -> list[dict]:
    """HF 文件可能是 JSON 数组或 JSON Lines，两种都兼容。"""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


ZIP_META = ("dev_tables.json", "mini_dev_sqlite.json", "mini_dev_sqlite_gold.sql")


def extract_sqlite(zip_path: Path) -> None:
    """只解出 *.sqlite 和 database_description/*.csv，放到 data/dev_databases/<db_id>/。

    另把 dev_tables.json（表结构与外键）和包内题目文件解到 data/raw/zip/，用于和 HF 版对比。
    """
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if "__MACOSX" not in parts and parts[-1] in ZIP_META and "MINIDEV" in parts:
                target = RAW / "zip" / parts[-1]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
                continue
            if "dev_databases" not in parts or info.is_dir():
                continue
            rel = Path(*parts[parts.index("dev_databases") + 1 :])
            if rel.name.startswith(".") or "__MACOSX" in parts:
                continue
            if rel.suffix not in {".sqlite", ".csv"}:
                continue
            target = (DB_DIR / rel).resolve()
            if not target.is_relative_to(DB_DIR.resolve()):
                raise ValueError(f"压缩包条目越出目标目录：{info.filename}")
            if target.exists() and target.stat().st_size == info.file_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def main() -> int:
    """可选参数 --zip <路径>：直接使用本机已有的 minidev.zip（复现时免重复下载 800MB）。"""
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return 0
    RAW.mkdir(parents=True, exist_ok=True)
    zip_path = ZIP_PATH
    if "--zip" in sys.argv:
        i = sys.argv.index("--zip")
        if i + 1 >= len(sys.argv):
            print("用法：--zip <minidev.zip 路径>")
            return 2
        zip_path = Path(sys.argv[i + 1]).resolve()
    hf_raw = RAW / "mini_dev_sqlite_hf.json"
    download(HF_FILE, hf_raw)
    questions = load_questions(hf_raw)
    QUESTIONS.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")

    if zip_path == ZIP_PATH:
        download(ZIP_URL, ZIP_PATH)
    extract_sqlite(zip_path)

    db_ids = sorted({q["db_id"] for q in questions})
    missing = [d for d in db_ids if not (DB_DIR / d / f"{d}.sqlite").exists()]
    print(f"题目 {len(questions)} 道，涉及数据库 {len(db_ids)} 个")
    if missing:
        print(f"缺少数据库：{missing}")
        return 1
    print("数据就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
