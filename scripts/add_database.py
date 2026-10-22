"""接入一个新的 SQLite 数据库：复制到 data/dev_databases/<db_id>/，并生成列说明草稿。

- 只复制，不修改源库；目标已存在时拒绝覆盖
- 列说明草稿：从建表语句的行尾注释（如 `tier TEXT -- normal / vip`）提取取值说明，
  写成 BIRD 格式的 database_description/<table>.csv，之后人工补全
- 系统会把这些说明放进增强 schema（取值说明对"取值写错"类错误最有用）

用法：uv run python scripts/add_database.py <db_id> <源库路径>
完整流程见 skills/chatbi-add-database/SKILL.md
"""

from __future__ import annotations

import csv
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datapilot import config  # noqa: E402

COMMENT_RE = re.compile(r'^\s*"?(\w+)"?\s+[^,]*?(?:,)?\s*--\s*(.+?)\s*$')
FIELDS = [
    "original_column_name",
    "column_name",
    "column_description",
    "data_format",
    "value_description",
]


def _column_comments(create_sql: str) -> dict[str, str]:
    out = {}
    for line in create_sql.splitlines():
        m = COMMENT_RE.match(line)
        if m and m.group(1).upper() not in {"PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE"}:
            out[m.group(1)] = m.group(2)
    return out


def add_database(db_id: str, source: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db_id):
        raise ValueError(f"db_id 只能包含字母、数字、下划线：{db_id}")
    target_dir = config.DB_DIR / db_id
    target = target_dir / f"{db_id}.sqlite"
    if target.exists():
        raise FileExistsError(f"{target} 已存在，不覆盖")
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

    desc_dir = target_dir / "database_description"
    desc_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
        ).fetchall()
        for name, sql in tables:
            comments = _column_comments(sql or "")
            cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            with (desc_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                for c in cols:
                    w.writerow(
                        {
                            "original_column_name": c[1],
                            "column_name": "",
                            "column_description": "",  # 待人工补全
                            "data_format": (c[2] or "").lower(),
                            "value_description": comments.get(c[1], ""),
                        }
                    )
    finally:
        conn.close()
    return target


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    target = add_database(sys.argv[1], Path(sys.argv[2]))
    print(f"已接入：{target.relative_to(ROOT)}")
    print(
        f"列说明草稿：{target.parent.name}/database_description/（请人工补全 column_description）"
    )


if __name__ == "__main__":
    main()
