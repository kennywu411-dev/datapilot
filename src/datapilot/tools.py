"""数据库工具：Agent 和 MCP Server 共用。全部只读。

表名、列名会拼进 SQL，所以先和 sqlite_master / PRAGMA 的真实名字比对，不在白名单里直接拒绝。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
from functools import cache

from datapilot import config, rls
from datapilot.sql_guard import SQLRejected, connect_readonly, run_readonly, table_columns

SAMPLE_ROWS = 3
SEARCH_TIMEOUT_S = 5.0
CELL_MAX_CHARS = 200


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _db(db_id: str):
    if db_id not in list_databases():
        raise ValueError(f"未知数据库：{db_id}")
    return config.db_path(db_id)


@cache
def _tables_cached(db_file: str) -> tuple[tuple[str, int], ...]:
    """表名与列数。表结构是静态的，按库文件路径缓存，避免每次工具调用都遍历全部表。"""
    conn = connect_readonly(db_file)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name != 'sqlite_sequence' ORDER BY name"
            )
        ]
        return tuple(
            (n, len(conn.execute(f"PRAGMA table_info({_q(n)})").fetchall())) for n in names
        )
    finally:
        conn.close()


def _table(db_id: str, table: str) -> str:
    if table not in {name for name, _ in _tables_cached(str(_db(db_id)))}:
        raise ValueError(f"未知表：{table}")
    return table


def _escape_like(s: str) -> str:
    """keyword 里的 % 和 _ 按字面匹配，不当通配符。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_text(col_type: str) -> bool:
    t = col_type.upper()
    return "CHAR" in t or "TEXT" in t or "CLOB" in t


def _clip(v):
    if isinstance(v, str) and len(v) > CELL_MAX_CHARS:
        return v[:CELL_MAX_CHARS] + "…"
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    return v


def list_databases() -> list[str]:
    if not config.DB_DIR.exists():
        return []
    return sorted(p.name for p in config.DB_DIR.iterdir() if (p / f"{p.name}.sqlite").exists())


def list_tables(db_id: str) -> list[dict]:
    return [{"table": n, "columns": c} for n, c in _tables_cached(str(_db(db_id)))]


@cache
def _descriptions(db_id: str, table: str) -> dict[str, dict]:
    """BIRD 自带的列说明（database_description/<table>.csv），编码不统一，逐个尝试。"""
    path = config.DB_DIR / db_id / "database_description" / f"{table}.csv"
    if not path.exists():
        return {}
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("original_column_name") or "").strip()
        if name:
            out[name.lower()] = {
                "description": (row.get("column_description") or "").strip(),
                "value_description": (row.get("value_description") or "").strip(),
            }
    return out


def source(db_id: str, table: str, role: str | None) -> str:
    """表在该角色下的读取来源：不受限时是表名，受限时是只含可见列、带行过滤的子查询。"""
    policy = rls.policy_for(db_id, role)
    return rls.source_sql(table, table_columns(str(_db(db_id).resolve()))[table], policy)


def describe_table(db_id: str, table: str, role: str | None = None) -> dict:
    """表结构与样例。受限角色下：不可见的列只给列名并标 restricted（不给说明、取值、样例），
    样例只取可见列和可见的行。"""
    table = _table(db_id, table)
    policy = rls.policy_for(db_id, role)
    hidden = policy.hidden_in(table) if policy else set()
    desc = _descriptions(db_id, table)
    conn = connect_readonly(_db(db_id), policy)
    try:
        cols = conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()
        fks = conn.execute(f"PRAGMA foreign_key_list({_q(table)})").fetchall()
        sample = conn.execute(
            f"SELECT * FROM {source(db_id, table, role)} LIMIT {SAMPLE_ROWS}"
        ).fetchall()
    finally:
        conn.close()
    columns = []
    for _cid, name, ctype, _notnull, _default, pk in cols:
        if name.lower() in hidden:
            columns.append(
                {
                    "name": name,
                    "type": ctype,
                    "primary_key": bool(pk),
                    "description": "",
                    "value_description": "",
                    "restricted": True,
                }
            )
            continue
        d = desc.get(name.lower(), {})
        columns.append(
            {
                "name": name,
                "type": ctype,
                "primary_key": bool(pk),
                "description": d.get("description", ""),
                "value_description": d.get("value_description", ""),
            }
        )
    return {
        "table": table,
        "columns": columns,
        "foreign_keys": [{"column": f[3], "ref_table": f[2], "ref_column": f[4]} for f in fks],
        "sample_rows": [[_clip(v) for v in r] for r in sample],
    }


def search_values(
    db_id: str,
    keyword: str,
    table: str | None = None,
    column: str | None = None,
    limit: int = 10,
    role: str | None = None,
) -> dict:
    """在文本列里模糊查找真实取值（大小写不敏感的 LIKE）。用户说"女性"，库里可能存的是 'F'。
    受限角色下不搜不可见的列，只在可见的行里搜（防止用它探测敏感值）。"""
    path = _db(db_id)
    policy = rls.policy_for(db_id, role)
    tables = [_table(db_id, table)] if table else [t["table"] for t in list_tables(db_id)]
    conn = connect_readonly(path)
    targets = []
    try:
        for t in tables:
            cols = conn.execute(f"PRAGMA table_info({_q(t)})").fetchall()
            names = {c[1] for c in cols}
            hidden = policy.hidden_in(t) if policy else set()
            if column:
                if column not in names:
                    raise ValueError(f"未知列：{t}.{column}")
                if column.lower() in hidden:
                    raise ValueError(f"当前角色（{policy.label}）无权访问 {t}.{column}")
                targets.append((t, column))
            else:
                targets += [
                    (t, c[1]) for c in cols if _is_text(c[2]) and c[1].lower() not in hidden
                ]
    finally:
        conn.close()

    matches, skipped = [], []
    deadline = time.monotonic() + SEARCH_TIMEOUT_S
    conn = connect_readonly(path, policy)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        for t, c in targets:
            if len(matches) >= limit:
                break
            sql = (
                f"SELECT DISTINCT {_q(c)} FROM {source(db_id, t, role)} "
                f"WHERE {_q(c)} LIKE ? ESCAPE '\\' "
                f"LIMIT {limit - len(matches)}"
            )
            try:
                rows = conn.execute(sql, (f"%{_escape_like(keyword)}%",)).fetchall()
            except sqlite3.OperationalError:  # 超时（interrupted）或个别列出错，跳过并告知
                skipped.append(f"{t}.{c}")
                continue
            matches += [{"table": t, "column": c, "value": _clip(r[0])} for r in rows]
    finally:
        conn.close()
    return {"keyword": keyword, "matches": matches[:limit], "skipped": skipped}


def run_sql(db_id: str, sql: str, max_rows: int = 50, role: str | None = None) -> dict:
    """只读执行单条 SELECT。错误以结构化结果返回（不抛异常），方便模型据此纠错。"""
    if max_rows < 0:
        return {"error": "bad_arguments", "message": f"max_rows 不能为负：{max_rows}"}
    try:
        res = run_readonly(_db(db_id), sql, max_rows=max_rows, policy=rls.policy_for(db_id, role))
    except SQLRejected as e:
        return {"error": "rejected", "message": str(e)}
    except TimeoutError as e:
        return {"error": "timeout", "message": str(e)}
    except sqlite3.Error as e:
        return {"error": "sql_error", "message": str(e)}
    return {
        "columns": res.columns,
        "rows": [[_clip(v) for v in r] for r in res.rows],
        "row_count": len(res.rows),
        "truncated": res.truncated,
        "elapsed_s": round(res.elapsed_s, 3),
    }
