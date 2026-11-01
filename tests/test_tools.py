"""数据库工具（Agent 与 MCP Server 共用）测试。"""

import asyncio
import sqlite3

import pytest

from datapilot import tools


@pytest.fixture
def db_dir(tmp_path, monkeypatch):
    d = tmp_path / "dbs"
    (d / "shop" / "database_description").mkdir(parents=True)
    conn = sqlite3.connect(d / "shop" / "shop.sqlite")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, gender TEXT)")
    conn.execute(
        'CREATE TABLE "order" (id INTEGER, user_id INTEGER, amount REAL, '
        "FOREIGN KEY (user_id) REFERENCES users(id))"
    )
    conn.executemany(
        "INSERT INTO users VALUES (?, ?, ?)",
        [(1, "Alice Smith", "F"), (2, "Bob", "M"), (3, "Carol", "F")],
    )
    conn.executemany('INSERT INTO "order" VALUES (?, ?, ?)', [(1, 1, 9.5), (2, 2, 20.0)])
    conn.commit()
    conn.close()
    (d / "shop" / "database_description" / "users.csv").write_text(
        "original_column_name,column_name,column_description,data_format,value_description\n"
        "gender,,user gender,text,F: female; M: male\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("datapilot.config.DB_DIR", d)
    return d


def test_list_databases(db_dir):
    assert tools.list_databases() == ["shop"]


def test_list_tables(db_dir):
    out = tools.list_tables("shop")
    assert [t["table"] for t in out] == ["order", "users"]
    assert next(t for t in out if t["table"] == "users")["columns"] == 3


def test_describe_table_with_fk_samples_and_description(db_dir):
    out = tools.describe_table("shop", "order")
    assert [c["name"] for c in out["columns"]] == ["id", "user_id", "amount"]
    assert out["foreign_keys"] == [{"column": "user_id", "ref_table": "users", "ref_column": "id"}]
    assert len(out["sample_rows"]) == 2
    users = tools.describe_table("shop", "users")
    gender = next(c for c in users["columns"] if c["name"] == "gender")
    assert "F: female" in gender["value_description"]


def test_unknown_db_or_table_is_rejected(db_dir):
    with pytest.raises(ValueError, match="未知数据库"):
        tools.list_tables("../etc")
    with pytest.raises(ValueError, match="未知表"):
        tools.describe_table("shop", 'users"; DROP TABLE users; --')


def test_search_values_fuzzy(db_dir):
    out = tools.search_values("shop", "smith")
    assert {"table": "users", "column": "name", "value": "Alice Smith"} in out["matches"]


def test_search_values_restricted_to_column(db_dir):
    out = tools.search_values("shop", "F", table="users", column="gender")
    assert {m["value"] for m in out["matches"]} == {"F"}
    with pytest.raises(ValueError, match="未知列"):
        tools.search_values("shop", "F", table="users", column="nope")


def test_search_values_escapes_like_wildcards(db_dir):
    # 'a_' 不应把 _ 当通配符匹配到 'Alice Smith' 里的 "al" 之类
    out = tools.search_values("shop", "e_S")
    assert out["matches"] == []
    out = tools.search_values("shop", "%")
    assert out["matches"] == []


def test_run_sql_unterminated_quote_is_structured_error(db_dir):
    out = tools.run_sql("shop", "SELECT 'abc")
    assert out["error"] == "sql_error"


def test_run_sql_clamps_negative_max_rows(db_dir):
    out = tools.run_sql("shop", "SELECT * FROM users", max_rows=-5)
    assert out["error"] == "bad_arguments"


def test_run_sql_ok_and_rejected(db_dir):
    out = tools.run_sql("shop", "SELECT name FROM users WHERE gender = 'F' ORDER BY id")
    assert out["columns"] == ["name"] and out["rows"] == [["Alice Smith"], ["Carol"]]
    bad = tools.run_sql("shop", "DELETE FROM users")
    assert bad["error"] == "rejected"
    err = tools.run_sql("shop", "SELECT nope FROM users")
    assert err["error"] == "sql_error" and "no such column" in err["message"]


def test_run_sql_truncates(db_dir):
    out = tools.run_sql("shop", "SELECT * FROM users", max_rows=2)
    assert len(out["rows"]) == 2 and out["truncated"] is True


def test_mcp_server_exposes_tools(db_dir):
    from mcp import Client

    from datapilot.mcp_server import server

    async def go():
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            res = await client.call_tool(
                "run_sql", {"db_id": "shop", "sql": "SELECT count(*) AS n FROM users"}
            )
            return names, res

    names, res = asyncio.run(go())
    assert names == {"list_databases", "list_tables", "describe_table", "search_values", "run_sql"}
    # MCP 2.x 中返回普通 dict 的工具只给 JSON 文本，不给 structured_content
    import json

    data = res.structured_content or json.loads(res.content[0].text)
    assert res.is_error is False and data["rows"] == [[3]]
