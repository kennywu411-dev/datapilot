"""只读 SQL 执行层：分层防御。

1. 静态检查：sqlglot 解析，只放行单条 SELECT / WITH ... SELECT；
   sqlglot 解析不了的（不一定非法）不拒绝，交给 SQLite 执行并由下面两层兜底，
   这样评测口径与官方一致，写错的 SQL 也能拿到 SQLite 原生报错去纠错；
2. 只读连接：`file:...?mode=ro`，写库文件会被拒绝；
3. 授权回调：`set_authorizer` 只放行读操作。只读连接单独拦不住 ATTACH、
   PRAGMA、CREATE TEMP TABLE（问题日志 P6），这一层补上；
4. 超时：`set_progress_handler` 定期检查耗时，超时中断；
5. 行数上限：`fetchmany`，避免几万行结果撑爆模型上下文。
6. 行列级权限（传了 policy 时，v1.4）：语法树改写 + 按列拒绝的授权回调，见 rls.py。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from datapilot import rls

DEFAULT_TIMEOUT_S = 30.0  # 与官方评测 meta_time_out 一致
DEFAULT_MAX_ROWS = 1000


class SQLRejected(Exception):
    """SQL 未通过安全检查，或被只读连接拒绝。"""


class SQLUnparseable(SQLRejected):
    """sqlglot 无法解析。run_readonly 会继续交给 SQLite（由只读连接和授权回调兜底）。"""


# 授权回调只放行这些操作：读表、SELECT、函数调用、递归 CTE
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


# 只读的元数据 PRAGMA（tools.describe_table 要用），且不能带赋值
_ALLOWED_PRAGMAS = {"table_info", "table_xinfo", "foreign_key_list", "index_list", "index_info"}


def _authorizer(action: int, arg1, arg2, _db, _source) -> int:
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _ALLOWED_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


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
    except SqlglotError as e:  # 含 ParseError 与 TokenError（后者不是前者子类）
        raise SQLUnparseable(f"SQL 无法解析：{e}") from e
    if len(statements) != 1:
        raise SQLRejected(f"只允许单条语句，收到 {len(statements)} 条")
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise SQLRejected(f"只允许 SELECT 查询，收到 {type(stmt).__name__}")
    # WITH t AS (...) DELETE ... 之类：查询树里不能出现任何写操作节点
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command)
    if any(stmt.find_all(*forbidden)):
        raise SQLRejected("查询中包含写操作")


def connect_readonly(db_path: str | Path, policy: rls.Policy | None = None) -> sqlite3.Connection:
    """只读连接 + 只放行读操作的授权回调；文件不存在时报错而不是新建空库。
    传了 policy 时，授权回调还会拒绝读该角色不可见的列（行列级权限的兜底层）。"""
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.set_authorizer(_authorizer if policy is None else rls.make_authorizer(policy, _authorizer))
    return conn


@cache
def table_columns(db_path: str) -> dict[str, list[str]]:
    """真实表名 → 列名（权限改写用）。表结构是静态的，按库文件缓存。"""
    conn = connect_readonly(db_path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {
            n: [c[0] for c in conn.execute("SELECT name FROM pragma_table_info(?)", (n,))]
            for n in names
        }
    finally:
        conn.close()


def apply_policy(db_path: str | Path, sql: str, policy: rls.Policy | None) -> str:
    """按角色改写 SQL；违反权限抛 SQLRejected（消息说明哪一列无权访问）。"""
    if policy is None:
        return sql
    try:
        return rls.rewrite(sql, policy, table_columns(str(Path(db_path).resolve())))
    except rls.Unparseable as e:
        raise SQLUnparseable(str(e)) from e
    except rls.AccessDenied as e:
        raise SQLRejected(str(e)) from e


def run_readonly(
    db_path: str | Path,
    sql: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    skip_guard: bool = False,
    policy: rls.Policy | None = None,
) -> QueryResult:
    """在只读连接上执行 SQL。

    skip_guard 只给测试和评测标准答案用：标准 SQL 不需要静态检查，但仍走只读连接。
    超时抛 TimeoutError；SQL 本身错误原样抛 sqlite3 异常，方便把报错反馈给模型纠错。
    """
    if max_rows is not None and max_rows < 0:
        raise ValueError(f"max_rows 不能为负：{max_rows}")
    if not skip_guard:
        try:
            check_sql(sql)
        except SQLUnparseable:
            pass  # 交给 SQLite：语法真错会得到原生报错，写操作由只读连接和授权回调拦截
    sql = apply_policy(db_path, sql, policy)  # 受限角色下无法解析的 SQL 在这里被拒绝

    conn = connect_readonly(db_path, policy)
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
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if msg == "interrupted":
            raise TimeoutError(f"查询超过 {timeout_s:g} 秒被中断") from e
        if "readonly" in msg or "not authorized" in msg:
            raise SQLRejected(f"只读连接拒绝非读操作：{msg}") from e
        raise
    finally:
        conn.close()
    return QueryResult(columns, rows, truncated, time.monotonic() - start)
