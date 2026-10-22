"""只读 SQL 执行层：分层防御。

1. 静态检查：sqlglot 解析，只放行单条 SELECT / WITH ... SELECT；
2. 只读连接：`file:...?mode=ro`，即使静态检查被绕过，写操作也会被 SQLite 拒绝；
3. 超时：`set_progress_handler` 定期检查耗时，超时中断；
4. 行数上限：`fetchmany`，避免几万行结果撑爆模型上下文。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DEFAULT_TIMEOUT_S = 30.0  # 与官方评测 meta_time_out 一致
DEFAULT_MAX_ROWS = 1000


class SQLRejected(Exception):
    """SQL 未通过安全检查，或被只读连接拒绝。"""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_s: float


def check_sql(sql: str) -> None:
    """只放行单条 SELECT（含 WITH、UNION 等查询）。不通过则抛 SQLRejected。"""
    if not sql or not sql.strip():
        raise SQLRejected("SQL 为空")
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except ParseError as e:
        raise SQLRejected(f"SQL 无法解析：{e}") from e
    if len(statements) != 1:
        raise SQLRejected(f"只允许单条语句，收到 {len(statements)} 条")
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise SQLRejected(f"只允许 SELECT 查询，收到 {type(stmt).__name__}")
    # WITH t AS (...) DELETE ... 之类：查询树里不能出现任何写操作节点
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command)
    if any(stmt.find_all(*forbidden)):
        raise SQLRejected("查询中包含写操作")


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """只读连接；文件不存在时报错而不是新建空库。"""
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def run_readonly(
    db_path: str | Path,
    sql: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    skip_guard: bool = False,
) -> QueryResult:
    """在只读连接上执行 SQL。

    skip_guard 只给测试和评测标准答案用：标准 SQL 不需要静态检查，但仍走只读连接。
    超时抛 TimeoutError；SQL 本身错误原样抛 sqlite3 异常，方便把报错反馈给模型纠错。
    """
    if not skip_guard:
        check_sql(sql)

    conn = connect_readonly(db_path)
    start = time.monotonic()
    deadline = start + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        if max_rows is None:
            rows = cur.fetchall()
            truncated = False
        else:
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
    except sqlite3.OperationalError as e:
        msg = str(e)
        if msg == "interrupted":
            raise TimeoutError(f"查询超过 {timeout_s:g} 秒被中断") from e
        if "readonly" in msg:
            raise SQLRejected(f"只读连接拒绝写操作：{msg}") from e
        raise
    finally:
        conn.close()
    return QueryResult(columns, rows, truncated, time.monotonic() - start)
