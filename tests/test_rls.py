"""行列级权限：每种绕过写法一个测试。执行层（改写 + 授权回调）、展示层、探测工具都要拦住。"""

import sqlite3

import pytest

from datapilot import rls, tools
from datapilot.config import db_path
from datapilot.sql_guard import SQLRejected, connect_readonly, run_readonly


@pytest.fixture
def mini(tmp_path, monkeypatch):
    """小电商库：users / products / orders / tickets，两个城市；角色 ops（看不到成本和姓名）、
    sh（同 ops，且只看上海的用户、订单、售后）。"""
    d = tmp_path / "dbs"
    (d / "mini").mkdir(parents=True)
    conn = sqlite3.connect(d / "mini" / "mini.sqlite")
    conn.executescript(
        """
        CREATE TABLE users (user_id TEXT PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT, cost REAL, price REAL);
        CREATE TABLE orders (order_id TEXT PRIMARY KEY, user_id TEXT, sku TEXT, amount REAL);
        CREATE TABLE tickets (ticket_id TEXT PRIMARY KEY, order_id TEXT, refund REAL);
        INSERT INTO users VALUES ('U1', '张秘密', '上海'), ('U2', '李秘密', '北京');
        INSERT INTO products VALUES ('P1', '耳机', 111.11, 300), ('P2', '面霜', 222.22, 200);
        INSERT INTO orders VALUES ('O1', 'U1', 'P1', 300), ('O2', 'U2', 'P2', 200),
                                  ('O3', 'U2', 'P1', 300);
        INSERT INTO tickets VALUES ('T1', 'O1', 300), ('T2', 'O2', 200);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("datapilot.config.DB_DIR", d)
    tools._tables_cached.cache_clear()
    sensitive = {"products": {"cost"}, "users": {"name"}}
    sh_rows = {
        "users": "city = '上海'",
        "orders": "user_id IN (SELECT user_id FROM users WHERE city = '上海')",
        "tickets": "order_id IN (SELECT order_id FROM orders WHERE user_id IN "
        "(SELECT user_id FROM users WHERE city = '上海'))",
    }
    monkeypatch.setitem(
        rls.ROLES,
        "mini",
        {
            "analyst": rls.Policy("分析师"),
            "ops": rls.Policy("运营", hidden=sensitive),
            "sh": rls.Policy("上海区域经理", hidden=sensitive, rows=sh_rows),
        },
    )
    return db_path("mini")


def q(path, sql, role):
    return run_readonly(path, sql, policy=rls.policy_for("mini", role)).rows


# ---------- 列权限：每种写法都读不到 cost / users.name ----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT cost FROM products",  # 直接写
        "SELECT p.cost FROM products AS p",  # 别名
        "SELECT x FROM (SELECT cost AS x FROM products)",  # 子查询
        "SELECT o.order_id, p.cost FROM orders o JOIN products p ON o.sku = p.sku",  # JOIN
        "SELECT price FROM products UNION SELECT cost FROM products",  # UNION
        "SELECT cost FROM main.products",  # 直接写 main.表名
        "SELECT name FROM users",  # 敏感文本列
        "WITH t AS (SELECT * FROM main.products) SELECT cost FROM t",  # 自己写 CTE
        "WITH products AS (SELECT * FROM main.products) SELECT cost FROM products",  # 同名 CTE
        "SELECT sku FROM products WHERE cost > 150",  # 只在 WHERE 里用（侧信道）
        "SELECT sku FROM products ORDER BY cost DESC LIMIT 1",  # 只在 ORDER BY 里用
    ],
)
def test_hidden_columns_unreachable(mini, sql):
    with pytest.raises((SQLRejected, sqlite3.OperationalError)):
        q(mini, sql, "ops")


def test_select_star_drops_hidden_columns(mini):
    res = run_readonly(
        mini, "SELECT * FROM products ORDER BY sku", policy=rls.policy_for("mini", "ops")
    )
    assert res.columns == ["sku", "name", "price"]
    assert all(111.11 not in r and 222.22 not in r for r in res.rows)
    star = run_readonly(
        mini,
        "SELECT * FROM orders o JOIN users u ON o.user_id = u.user_id",
        policy=rls.policy_for("mini", "ops"),
    )
    assert "张秘密" not in str(star.rows) and "name" not in star.columns


def test_same_name_column_in_other_table_still_visible(mini):
    """products.name 可见，users.name 不可见：同名列不能误伤。"""
    assert q(mini, "SELECT name FROM products ORDER BY sku", "ops") == [("耳机",), ("面霜",)]


def test_unqualified_column_with_join_visible_side(mini):
    rows = q(
        mini,
        "SELECT p.name, u.city FROM orders o JOIN products p ON o.sku = p.sku "
        "JOIN users u ON o.user_id = u.user_id ORDER BY o.order_id",
        "ops",
    )
    assert rows[0] == ("耳机", "上海")


# ---------- 行权限：上海经理只能看到上海的数据 ----------


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT COUNT(*) FROM orders", [(1,)]),
        ("SELECT COUNT(*) FROM main.orders", [(1,)]),
        ("SELECT COUNT(*) FROM orders o JOIN users u ON o.user_id = u.user_id", [(1,)]),
        ("SELECT COUNT(*) FROM (SELECT * FROM orders)", [(1,)]),
        ("SELECT COUNT(*) FROM tickets", [(1,)]),
        ("SELECT COUNT(*) FROM users WHERE city = '北京'", [(0,)]),
        ("SELECT COUNT(*) FROM orders WHERE user_id = 'U2'", [(0,)]),
        ("SELECT (SELECT COUNT(*) FROM orders) + (SELECT COUNT(*) FROM tickets)", [(2,)]),
        ("SELECT order_id FROM orders UNION SELECT order_id FROM tickets", [("O1",)]),
    ],
)
def test_row_filter_applies_everywhere(mini, sql, expected):
    assert q(mini, sql, "sh") == expected


def test_products_not_row_filtered(mini):
    assert q(mini, "SELECT COUNT(*) FROM products", "sh") == [(2,)]


# ---------- 元数据、无法解析、未知角色：一律拒绝 ----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sql FROM sqlite_master",
        "SELECT sql FROM sqlite_schema",
        "SELECT name FROM pragma_table_info('products')",
        "SELECT cost FROM temp.products",
    ],
)
def test_metadata_and_other_schemas_rejected(mini, sql):
    with pytest.raises((SQLRejected, sqlite3.OperationalError)):
        q(mini, sql, "ops")


def test_unparseable_sql_rejected_under_role(mini):
    """无法解析就无法改写：受限角色下直接拒绝，不交给 SQLite 碰运气。"""
    with pytest.raises(SQLRejected):
        q(mini, "SELECT cost FROM products WHERE ((", "ops")


def test_unknown_role_fails_closed(mini):
    with pytest.raises(ValueError):
        rls.policy_for("mini", "ceo")
    with pytest.raises(ValueError):
        rls.policy_for("superhero_no_roles", "ops")  # 没定义角色的库不能指定角色
    assert rls.policy_for("anydb", None) is None
    assert rls.policy_for("mini", "analyst") is None  # 不受限角色等同无策略，SQL 原样执行


def test_analyst_sql_untouched(mini):
    assert q(mini, "SELECT cost FROM products ORDER BY sku", "analyst") == [(111.11,), (222.22,)]


# ---------- 引擎层兜底：即使跳过改写，授权回调也读不到敏感列 ----------


def test_authorizer_backstop_without_rewrite(mini):
    conn = connect_readonly(mini, policy=rls.policy_for("mini", "ops"))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
            conn.execute("SELECT cost FROM products").fetchall()
        assert conn.execute("SELECT price FROM products ORDER BY sku").fetchall() == [
            (300,),
            (200,),
        ]
    finally:
        conn.close()


def test_hidden_column_error_message_is_clear(mini):
    """模型据此能如实告诉用户"无权访问"，而不是当成 SQL 写错去乱改。"""
    with pytest.raises(SQLRejected, match="无权访问.*products.cost"):
        q(mini, "SELECT cost FROM products", "ops")


# ---------- 展示层与探测工具 ----------


def test_describe_table_hides_columns_and_samples(mini):
    info = tools.describe_table("mini", "products", role="ops")
    visible = [c["name"] for c in info["columns"] if not c.get("restricted")]
    assert visible == ["sku", "name", "price"]
    assert any(c["name"] == "cost" and c["restricted"] for c in info["columns"])
    assert "111.11" not in str(info["sample_rows"])
    users = tools.describe_table("mini", "users", role="sh")
    assert "张秘密" not in str(users) and "李秘密" not in str(users)


def test_search_values_cannot_probe(mini):
    assert tools.search_values("mini", "秘密", role="ops")["matches"] == []
    with pytest.raises(ValueError, match="无权访问"):
        tools.search_values("mini", "秘密", table="users", column="name", role="ops")
    cities = tools.search_values("mini", "京", table="users", column="city", role="sh")
    assert cities["matches"] == []  # 北京的行对上海经理不可见


def test_run_sql_tool_returns_rejection(mini):
    out = tools.run_sql("mini", "SELECT cost FROM products", role="ops")
    assert out["error"] == "rejected" and "无权访问" in out["message"]


def test_schema_prompt_marks_restricted(mini):
    from datapilot.schema import enhanced_schema

    s = enhanced_schema("mini", None, "ops")
    assert "cost" in s and "无权访问" in s  # 列名可见、标明受限，模型能如实告知
    assert "111.11" not in s and "张秘密" not in s


def test_value_check_does_not_leak_hidden_or_filtered_values(mini):
    from datapilot.value_check import check_values

    issues = check_values("mini", "SELECT COUNT(*) FROM users WHERE city = '北京市'", role="sh")
    text = " ".join(issues)
    assert "similar existing values: '上海'" in text  # 候选取值只来自可见的行
    assert "'北京'" not in text


# ---------- 问数服务、API、MCP ----------


async def test_service_cache_is_per_role(mini):
    """分析师问过的成本价被缓存后，运营问同一句话不能直接拿到缓存（缓存泄露）。"""
    from datapilot.baseline import SQLAnswer
    from datapilot.llm import ScriptedLLM
    from datapilot.service import ChatService
    from datapilot.store import MemoryStore

    cost_sql = SQLAnswer(sql="SELECT sku, cost FROM products ORDER BY sku")
    llm = ScriptedLLM([cost_sql, "P1 成本 111.11。"] + [cost_sql] * 4, model="claude-haiku-5-5")
    svc = ChatService(MemoryStore(), llm_factory=lambda: llm)
    first = [e async for e in svc.ask("s1", "mini", "各商品成本价", role="analyst")]
    assert any(e["event"] == "table" and "111.11" in str(e["data"]) for e in first)
    second = [e async for e in svc.ask("s2", "mini", "各商品成本价", role="ops")]
    text = str(second)
    assert "111.11" not in text
    errors = [e["data"]["message"] for e in second if e["event"] == "error"]
    assert errors and "无权" in errors[0]


async def test_service_rejects_unknown_role(mini):
    from datapilot.service import ChatService
    from datapilot.store import MemoryStore

    svc = ChatService(MemoryStore(), llm_factory=lambda: None)
    out = [e async for e in svc.ask("s", "mini", "多少订单", role="ceo")]
    assert out[0]["event"] == "error" and "没有角色" in out[0]["data"]["message"]


async def test_api_schema_and_roles_follow_header(mini):
    import httpx

    from datapilot.api import create_app
    from datapilot.service import ChatService
    from datapilot.store import MemoryStore

    app = create_app(service=ChatService(MemoryStore(), llm_factory=lambda: None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        roles = (await c.get("/api/databases/mini/roles")).json()
        assert {r["role"] for r in roles} == {"analyst", "ops", "sh"}
        full = (await c.get("/api/databases/mini/schema")).json()
        limited = (await c.get("/api/databases/mini/schema", headers={"X-Role": "sh"})).json()
        assert "111.11" in str(full) and "111.11" not in str(limited)
        assert "李秘密" not in str(limited)
        bad = await c.get("/api/databases/mini/schema", headers={"X-Role": "ceo"})
        assert bad.status_code == 403


def test_mcp_tools_do_not_take_role_argument():
    """角色由启动环境固定；如果工具参数里有 role，模型就能自己给自己提权。"""
    import inspect

    from datapilot import mcp_server

    for fn in (mcp_server.describe_table, mcp_server.search_values, mcp_server.run_sql):
        target = getattr(fn, "fn", fn)
        assert "role" not in inspect.signature(target).parameters


@pytest.mark.parametrize(
    "sql",
    [
        # 用户 CTE 冒充过滤条件里的 users 表（代码审查实测曾能绕过）
        "WITH users AS (SELECT 'U2' AS user_id, '上海' AS city) SELECT COUNT(*) FROM orders",
        # 子查询里定义同名 CTE，让外层真实表被跳过改写（代码审查实测曾能绕过）
        "SELECT (WITH orders AS (SELECT 1) SELECT 1) AS z, COUNT(*) FROM orders",
        "WITH orders AS (SELECT * FROM main.orders) SELECT COUNT(*) FROM tickets",
    ],
)
def test_cte_shadowing_cannot_bypass_row_filter(mini, sql):
    with pytest.raises(SQLRejected, match="CTE 不能与数据表同名"):
        q(mini, sql, "sh")


def test_row_filter_tables_are_qualified():
    p = rls.Policy("x", rows={"orders": "user_id IN (SELECT user_id FROM users WHERE city = 'a')"})
    src = rls.source_sql("orders", ["order_id", "user_id"], p)
    assert "main.users" in src and "FROM users" not in src


def test_unparseable_gold_is_gold_error_not_denied(mini):
    from datapilot.ex import evaluate_one

    out = evaluate_one(
        mini, "SELECT 1", "SELECT cost FROM products WHERE ((", policy=rls.policy_for("mini", "ops")
    )
    assert out.status == "gold_error" and not out.correct
