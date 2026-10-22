"""数据库结构读取。"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from datapilot.sql_guard import connect_readonly


@cache
def schema_ddl(db_path: str | Path) -> str:
    """全部表的 CREATE TABLE 语句，与官方 generate_schema_prompt_sqlite(num_rows=None) 一致。"""
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
        ).fetchall()
    finally:
        conn.close()
    return "\n\n".join(r[0] for r in rows if r[0])
