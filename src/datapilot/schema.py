"""数据库结构读取：官方 DDL、增强版 schema（列说明 + 取值说明 + 示例值）、BM25 表检索。"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from rank_bm25 import BM25Okapi

from datapilot import rls, tools
from datapilot.sql_guard import connect_readonly

EXAMPLE_VALUES = 3
DESC_MAX_CHARS = 300


@cache
def schema_ddl(db_path: str | Path) -> str:
    """全部表的 CREATE TABLE 语句，与官方 generate_schema_prompt_sqlite(num_rows=None) 一致。"""
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
        ).fetchall()
    finally:
        conn.close()
    return "\n\n".join(sql for _, sql in rows if sql)


def ddl_for(db_id: str, tables: list[str]) -> str:
    """只保留指定表的官方 DDL（检索消融用）。"""
    conn = connect_readonly(tools.config.db_path(db_id))
    try:
        rows = dict(
            conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        )
    finally:
        conn.close()
    return "\n\n".join(rows[t] for t in tables if rows.get(t))


def _one_line(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= DESC_MAX_CHARS else s[:DESC_MAX_CHARS] + "…"


@cache
def _examples(db_id: str, table: str, column: str, role: str | None = None) -> tuple:
    conn = connect_readonly(tools.config.db_path(db_id), rls.policy_for(db_id, role))
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {tools._q(column)} FROM {tools.source(db_id, table, role)} "
            f"WHERE {tools._q(column)} IS NOT NULL AND {tools._q(column)} != '' "
            f"LIMIT {EXAMPLE_VALUES}"
        ).fetchall()
    finally:
        conn.close()
    return tuple(r[0] for r in rows)


def _render_table(db_id: str, table: str, role: str | None = None) -> str:
    info = tools.describe_table(db_id, table, role)
    entries: list[tuple[str, str]] = []  # (定义, 行尾注释)
    for c in info["columns"]:
        if c.get("restricted"):
            # 列名保留、标明受限：模型知道这列存在但不能用，才能如实告诉用户"无权查看"
            entries.append((f"{_ident(c['name'])} {c['type']}", "当前角色无权访问，不能查询"))
            continue
        notes = []
        if c["description"]:
            notes.append(_one_line(c["description"]))
        if c["value_description"]:
            notes.append("values: " + _one_line(c["value_description"]))
        if tools._is_text(c["type"] or ""):
            ex = [_sql_literal(tools._clip(v)) for v in _examples(db_id, table, c["name"], role)]
            if ex:
                notes.append("e.g. " + ", ".join(ex))
        pk = " PRIMARY KEY" if c["primary_key"] else ""
        entries.append((f"{_ident(c['name'])} {c['type']}{pk}", " | ".join(notes)))
    for fk in info["foreign_keys"]:
        ref = f"{_ident(fk['ref_table'])}({_ident(fk['ref_column'] or 'rowid')})"
        entries.append((f"FOREIGN KEY ({_ident(fk['column'])}) REFERENCES {ref}", ""))
    lines = []
    for i, (body, note) in enumerate(entries):
        sep = "," if i < len(entries) - 1 else ""
        lines.append(f"  {body}{sep}" + (f"  -- {note}" if note else ""))
    return f"CREATE TABLE {_ident(table)} (\n" + "\n".join(lines) + "\n);"


def _sql_literal(v) -> str:
    """示例值按 SQL 字面量输出（'O''Brien'），不用 Python repr，后者会给含单引号的
    字符串加双引号，而 SQLite 里双引号表示标识符，模型照抄会出错（代码审查发现）。"""
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def _ident(name: str) -> str:
    """普通标识符原样输出；带空格等特殊字符（BIRD 有 `Enrollment (K-12)`）或是 SQL 关键字
    （financial 库有表 `order`）的加双引号，否则模型照抄会写出语法错误的 SQL。"""
    if name.isidentifier() and name.upper() not in _SQL_KEYWORDS:
        return name
    return tools._q(name)


# SQLite 关键字（https://www.sqlite.org/lang_keywords.html）
_SQL_KEYWORDS = set(
    """ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE
    BEGIN BETWEEN BY CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE
    CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP DATABASE DEFAULT DEFERRABLE
    DEFERRED DELETE DESC DETACH DISTINCT DO DROP EACH ELSE END ESCAPE EXCEPT EXCLUDE EXCLUSIVE
    EXISTS EXPLAIN FAIL FILTER FIRST FOLLOWING FOR FOREIGN FROM FULL GENERATED GLOB GROUP GROUPS
    HAVING IF IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT INSTEAD INTERSECT INTO IS
    ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED NATURAL NO NOT NOTHING NOTNULL NULL
    NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN PRAGMA PRECEDING PRIMARY QUERY
    RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE RENAME REPLACE RESTRICT RETURNING
    RIGHT ROLLBACK ROW ROWS SAVEPOINT SELECT SET TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION
    TRIGGER UNBOUNDED UNION UNIQUE UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW
    WITH WITHOUT""".split()
)


@cache
def enhanced_schema(
    db_id: str, tables: tuple[str, ...] | None = None, role: str | None = None
) -> str:
    """DDL + 每列行尾注释：BIRD 列说明、取值说明、文本列示例值。
    受限角色下示例值只来自可见的行，无权访问的列只留列名并标明。"""
    names = tables or tuple(t["table"] for t in tools.list_tables(db_id))
    return "\n\n".join(_render_table(db_id, t, role) for t in names)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    # camelCase / snake_case 拆开：driverStandings -> driver standings
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return _TOKEN_RE.findall(text.lower())


@cache
def _bm25_index(db_id: str) -> tuple[list[str], BM25Okapi, tuple[tuple[str, str], ...]]:
    """BM25 索引与外键边（子表, 父表），同一个库只构建一次。"""
    names, docs, edges = [], [], []
    for t in tools.list_tables(db_id):
        info = tools.describe_table(db_id, t["table"])
        text = " ".join([t["table"]] + [f"{c['name']} {c['description']}" for c in info["columns"]])
        names.append(t["table"])
        docs.append(_tokens(text))
        edges += [(t["table"], fk["ref_table"]) for fk in info["foreign_keys"]]
    return names, BM25Okapi(docs), tuple(edges)


def retrieve_tables(db_id: str, question: str, evidence: str, k: int = 3) -> list[str]:
    """BM25 取 top-k 表，再沿外键（双向）补一跳关联表。"""
    names, bm25, edges = _bm25_index(db_id)
    scores = bm25.get_scores(_tokens(f"{question} {evidence}"))
    ranked = [n for _, n in sorted(zip(scores, names, strict=True), key=lambda x: -x[0])]
    picked = ranked[:k]
    neighbors = []
    for child, parent in edges:
        if child in picked and parent in names and parent not in picked:
            neighbors.append(parent)
        if parent in picked and child not in picked:
            neighbors.append(child)
    return picked + [n for n in dict.fromkeys(neighbors) if n not in picked]
