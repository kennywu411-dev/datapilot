"""只读安全层测试：先写测试，再写 src/datapilot/sql_guard.py。"""

import sqlite3

import pytest

from datapilot.sql_guard import QueryResult, SQLRejected, check_sql, run_readonly


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER, name TEXT, gender TEXT)")
    conn.executemany(
        "INSERT INTO users VALUES (?, ?, ?)",
        [(i, f"u{i}", "F" if i % 2 else "M") for i in range(1, 2001)],
    )
    conn.commit()
    conn.close()
    return path


# ---------- 静态检查：只放行单条 SELECT / WITH ----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users",
        "select count(*) from users where gender = 'F'",
        "WITH t AS (SELECT id FROM users) SELECT count(*) FROM t",
        "SELECT * FROM users;",  # 末尾分号允许
        "SELECT 'DROP TABLE users' AS s",  # 字符串里的关键字不算
    ],
)
def test_allows_single_select(sql):
    check_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE users",
        "DELETE FROM users",
        "UPDATE users SET name = 'x'",
        "INSERT INTO users VALUES (0, 'x', 'F')",
        "CREATE TABLE x (a INT)",
        "ALTER TABLE users ADD COLUMN x INT",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "PRAGMA writable_schema = 1",
        "VACUUM",
        "SELECT 1; DROP TABLE users",  # 多条语句
        "SELECT 1; SELECT 2",
        "WITH t AS (SELECT 1) DELETE FROM users",
        "",
        "   ",
    ],
)
def test_rejects_non_select(sql):
    with pytest.raises(SQLRejected):
        check_sql(sql)


def test_rejects_unparseable():
    with pytest.raises(SQLRejected):
        check_sql("SELEC * FORM users")


# ---------- 执行层：只读连接兜底 ----------


def test_select_returns_rows(db):
    res = run_readonly(db, "SELECT count(*) FROM users WHERE gender = 'F'")
    assert isinstance(res, QueryResult)
    assert res.columns == ["count(*)"]
    assert res.rows == [(1000,)]
    assert res.truncated is False


def test_readonly_connection_blocks_writes_even_if_guard_skipped(db):
    """即使绕过静态检查，只读连接也必须拦住写操作（分层防御）。"""
    with pytest.raises(SQLRejected):
        run_readonly(db, "DELETE FROM users", skip_guard=True)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM users").fetchone() == (2000,)
    conn.close()


def test_row_limit_truncates(db):
    res = run_readonly(db, "SELECT * FROM users", max_rows=100)
    assert len(res.rows) == 100
    assert res.truncated is True


def test_timeout_interrupts_slow_query(db):
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(TimeoutError):
        run_readonly(db, slow, timeout_s=0.5)


def test_sql_error_surfaces_message(db):
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        run_readonly(db, "SELECT not_a_col FROM users")


def test_missing_db_file_is_not_created(tmp_path):
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        run_readonly(missing, "SELECT 1")
    assert not missing.exists()
