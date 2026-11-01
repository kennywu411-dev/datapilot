"""值核对：SQL 里 `列 = '字面量'` / `列 IN ('a', 'b')` 的字面量在库里是否真实存在。

不存在时给出候选真实取值（大小写不敏感相等 → 包含该字面量 → 包含其中最长的词），
作为纠错反馈交给模型。只核对文本字面量，LIKE、函数包裹的列（如 strftime）不核对。
"""

from __future__ import annotations

import sqlite3
import time

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from datapilot import config, rls, tools
from datapilot.sql_guard import connect_readonly

MAX_CANDIDATES = 5
ENUM_MAX_VALUES = 20
CHECK_TIMEOUT_S = 5.0


def _comparisons(tree: exp.Expression) -> list[tuple[exp.Column, list[str]]]:
    out = []
    for node in tree.find_all(exp.EQ, exp.In):
        col, lits = None, []
        if isinstance(node, exp.EQ):
            left, right = node.left, node.right
            if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
                left, right = right, left
            if isinstance(left, exp.Column) and isinstance(right, exp.Literal) and right.is_string:
                col, lits = left, [right.this]
        else:
            if isinstance(node.this, exp.Column):
                vals = node.expressions
                if vals and all(isinstance(v, exp.Literal) and v.is_string for v in vals):
                    col, lits = node.this, [v.this for v in vals]
        if col is not None:
            out.append((col, lits))
    return out


def _resolve_tables(col: exp.Column, db_tables: dict[str, set[str]]) -> list[str]:
    """把列解析到真实表，只看列所在的那一层 SELECT 的 FROM / JOIN。

    BIRD 风格的 SQL 常在外层和子查询里重复使用 T1 指代不同的表，
    全局建别名表会让后出现的覆盖先出现的（代码审查发现）。
    """
    scope = col.find_ancestor(exp.Select)
    if scope is None:
        return []
    tables = [t for t in scope.find_all(exp.Table) if t.find_ancestor(exp.Select) is scope]
    alias_map = {}
    for t in tables:
        alias_map[t.name.lower()] = t.name
        if t.alias:
            alias_map[t.alias.lower()] = t.name
    if col.table:
        real = alias_map.get(col.table.lower())
        return [real] if real in db_tables else []
    return [t.name for t in tables if t.name in db_tables and col.name in db_tables[t.name]]


def _candidates(conn: sqlite3.Connection, q: str, column: str, lit: str) -> list:
    """q：表的读取来源（表名，或受限角色下按权限过滤后的子查询）。"""
    c = tools._q(column)
    like = f"SELECT DISTINCT {c} FROM {q} WHERE {c} LIKE ? ESCAPE '\\' LIMIT ?"
    probes = [
        (f"SELECT DISTINCT {c} FROM {q} WHERE {c} = ? COLLATE NOCASE LIMIT ?", lit),
        (like, f"%{tools._escape_like(lit)}%"),
    ]
    longest = max(lit.split(), key=len, default="")
    if longest != lit and len(longest) >= 3:
        probes.append((like, f"%{tools._escape_like(longest)}%"))
    for sql, arg in probes:
        rows = conn.execute(sql, (arg, MAX_CANDIDATES)).fetchall()
        if rows:
            return [r[0] for r in rows]
    # 兜底：低基数的枚举列（如 gender 存 'F' / 'M'）直接列出全部取值
    distinct = conn.execute(
        f"SELECT DISTINCT {c} FROM {q} WHERE {c} IS NOT NULL LIMIT ?", (ENUM_MAX_VALUES + 1,)
    ).fetchall()
    if len(distinct) <= ENUM_MAX_VALUES:
        return [r[0] for r in distinct]
    return []


def check_values(db_id: str, sql: str, role: str | None = None) -> list[str]:
    """返回问题描述列表（英文，直接作为纠错反馈）；无法解析或没有可核对的字面量时返回 []。"""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except SqlglotError:
        return []
    comps = _comparisons(tree)
    if not comps:
        return []
    # 受限角色下只核对可见的列，候选取值只来自可见的行（不能借纠错反馈泄露敏感值）
    db_tables = {
        t["table"]: {
            c["name"]
            for c in tools.describe_table(db_id, t["table"], role)["columns"]
            if not c.get("restricted")
        }
        for t in tools.list_tables(db_id)
    }
    issues = []
    conn = connect_readonly(config.db_path(db_id), rls.policy_for(db_id, role))
    deadline = time.monotonic() + CHECK_TIMEOUT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        for col, lits in comps:
            for table in _resolve_tables(col, db_tables):
                if col.name not in db_tables[table]:
                    continue
                src = tools.source(db_id, table, role)
                for lit in lits:
                    exists = conn.execute(
                        f"SELECT 1 FROM {src} WHERE {tools._q(col.name)} = ? LIMIT 1",
                        (lit,),
                    ).fetchone()
                    if exists:
                        continue
                    cands = _candidates(conn, src, col.name, lit)
                    hint = (
                        "similar existing values: " + ", ".join(repr(v) for v in cands)
                        if cands
                        else "no similar values found"
                    )
                    issues.append(f"Column {table}.{col.name} has no value {lit!r}; {hint}.")
    except sqlite3.OperationalError:
        # 超时（interrupted）或个别查询出错：核对是辅助手段，宁可少报也不误报
        return issues
    finally:
        conn.close()
    return issues
