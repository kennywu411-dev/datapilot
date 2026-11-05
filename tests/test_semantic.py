"""指标语义层：定义校验、指标匹配、口径注入、语义层直出 SQL、与行列级权限的兼容。"""

import asyncio
import sqlite3

import pytest

from datapilot import config, rls, tools
from datapilot.baseline import SQLAnswer
from datapilot.llm import ScriptedLLM
from datapilot.pipeline import FixedPipeline, PipelineConfig
from datapilot.semantic import (
    DimFilter,
    NotCompilable,
    SemanticLayer,
    SemanticPipeline,
    SemanticQuery,
    load_layer,
)
from datapilot.sql_guard import SQLRejected, run_readonly

SHOP = config.DATA_DIR / "dev_databases" / "shop_ops" / "shop_ops.sqlite"
needs_shop = pytest.mark.skipif(
    not SHOP.exists(), reason="先运行 scripts/gen_shop_ops.py 生成电商库"
)
VALID = "status NOT IN ('cancelled', 'pending')"


@pytest.fixture
def layer():
    return load_layer("shop_ops")


def rows(sql, role=None):
    return set(run_readonly(SHOP, sql, max_rows=None, policy=rls.policy_for("shop_ops", role)).rows)


# ---------- 定义文件 ----------


def test_layer_loads_and_unknown_db_has_none(layer):
    assert layer.db_id == "shop_ops"
    assert {"sales", "aov", "repurchase_rate", "gross_profit", "buyers"} <= set(layer.metrics)
    assert load_layer("no_such_db") is None


@needs_shop
def test_definitions_validate_against_real_db(layer):
    # 每个 SQL 片段里的列都存在、每个 simple 指标都能执行
    assert layer.validate(SHOP) == []


def test_validate_reports_unknown_column(tmp_path):
    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (amount REAL)")
    conn.close()
    bad = SemanticLayer.from_dict(
        {
            "db_id": "t",
            "metrics": {
                "m": {"name": "金额", "kind": "simple", "base": "orders", "expr": "SUM(orders.amt)"}
            },
        }
    )
    errors = bad.validate(db)
    assert errors and "orders.amt" in errors[0]


def test_unqualified_columns_rejected_at_load():
    with pytest.raises(ValueError, match="表名.列名"):
        SemanticLayer.from_dict(
            {
                "db_id": "t",
                "metrics": {"m": {"name": "金额", "kind": "simple", "base": "o", "expr": "SUM(x)"}},
            }
        )


# ---------- 匹配 ----------


@pytest.mark.parametrize(
    "question, expected",
    [
        ("2025 年各品类的销售额分别是多少？", ["sales"]),
        ("上个月 GMV 多少", ["sales"]),
        ("回头客比例是多少", ["repurchase_rate"]),
        ("各会员等级的客单价是多少？", ["aov"]),
        # 最长匹配：有效订单数不再拆出"订单数"
        ("2025 年有效订单数是多少", ["valid_order_count"]),
        ("已完结的售后工单，从申请到完结平均用了多少天？", ["ticket_closed", "resolve_days"]),
    ],
)
def test_match_by_synonym(layer, question, expected):
    assert [m.id for m in layer.match(question)] == expected
    assert all(m.via == "synonym" for m in layer.match(question))


def test_no_match_for_plain_lookup(layer):
    assert layer.match("一共有多少位超级会员？") == []


def test_fuzzy_fallback_when_no_synonym():
    lay = SemanticLayer.from_dict(
        {
            "db_id": "t",
            "metrics": {
                "sales": {
                    "name": "销售额",
                    "kind": "simple",
                    "synonyms": ["销售额"],
                    "base": "orders",
                    "expr": "SUM(orders.amount)",
                },
                "aov": {
                    "name": "客单价",
                    "kind": "simple",
                    "synonyms": ["客单价", "平均订单金额"],
                    "base": "orders",
                    "expr": "AVG(orders.amount)",
                },
            },
        }
    )
    got = lay.match("每笔订单平均金额是多少")
    assert [m.id for m in got] == ["aov"] and got[0].via == "fuzzy"
    assert lay.match("天气怎么样") == []


# ---------- 口径注入 ----------


def test_context_contains_definition_and_conventions(layer):
    ctx = layer.context(layer.match("2025 年各品类的销售额"))
    assert "销售额" in ctx and "orders.status NOT IN ('cancelled', 'pending')" in ctx
    assert "SUM(orders.amount)" in ctx
    assert "julianday" in ctx  # 全局约定每次都带
    assert layer.context([]).startswith("通用约定")


def test_context_hides_metric_needing_restricted_column(layer):
    m = layer.match("2025 年毛利最高的品牌")
    assert "products.cost" in layer.context(m)
    ctx = layer.context(m, role="ops")
    assert "SUM(orders.amount - products.cost" not in ctx
    assert "无权访问" in ctx


# ---------- 语义层直出 SQL ----------


@needs_shop
def test_compile_matches_gold_sales_by_category(layer):
    q = SemanticQuery(
        mappable=True,
        metric="sales",
        group_by=["category"],
        time_from="2025-01-01",
        time_to="2025-12-31",
    )
    gold = (
        "SELECT p.category, SUM(o.amount) FROM orders o JOIN products p ON o.sku = p.sku "
        f"WHERE o.created_at LIKE '2025%' AND o.{VALID} GROUP BY p.category"
    )
    assert rows(layer.compile(q)) == rows(gold)


@needs_shop
def test_compile_filter_order_limit_and_joins(layer):
    q = SemanticQuery(
        mappable=True,
        metric="sales",
        group_by=["city"],
        filters=[DimFilter(dimension="category", values=["beauty"])],
        time_from="2025-01-01",
        time_to="2025-12-31",
        order="desc",
        limit=1,
    )
    gold = (
        "SELECT u.city, SUM(o.amount) FROM orders o JOIN users u ON o.user_id = u.user_id "
        "JOIN products p ON o.sku = p.sku WHERE p.category = 'beauty' "
        f"AND o.created_at LIKE '2025%' AND o.{VALID} GROUP BY u.city ORDER BY 2 DESC LIMIT 1"
    )
    assert rows(layer.compile(q)) == rows(gold)


@needs_shop
def test_compile_round_hide_metric_and_ticket_base(layer):
    q = SemanticQuery(mappable=True, metric="aov", group_by=["tier"], round=2)
    gold = (
        "SELECT u.tier, ROUND(AVG(o.amount), 2) FROM orders o JOIN users u "
        f"ON o.user_id = u.user_id WHERE o.{VALID} GROUP BY u.tier"
    )
    assert rows(layer.compile(q)) == rows(gold)
    # 只要维度不要数值：毛利最高的品牌
    q = SemanticQuery(
        mappable=True,
        metric="gross_profit",
        group_by=["brand"],
        time_from="2025-01-01",
        time_to="2025-12-31",
        order="desc",
        limit=1,
        output_metric=False,
    )
    assert len(next(iter(rows(layer.compile(q))))) == 1
    # 售后表为主表，沿 tickets → orders → products 补 JOIN
    q = SemanticQuery(mappable=True, metric="ticket_count", group_by=["category"])
    gold = (
        "SELECT p.category, COUNT(*) FROM tickets t JOIN orders o ON t.order_id = o.order_id "
        "JOIN products p ON o.sku = p.sku GROUP BY p.category"
    )
    assert rows(layer.compile(q)) == rows(gold)


def test_compile_rejects_derived_unknown_and_bad_input(layer):
    with pytest.raises(NotCompilable):
        layer.compile(SemanticQuery(mappable=True, metric="repurchase_rate"))
    with pytest.raises(NotCompilable):
        layer.compile(SemanticQuery(mappable=True, metric="nope"))
    with pytest.raises(NotCompilable):
        layer.compile(SemanticQuery(mappable=True, metric="sales", group_by=["nope"]))
    with pytest.raises(NotCompilable):
        layer.compile(SemanticQuery(mappable=True, metric="sales", time_from="2025-01-01' OR 1"))
    with pytest.raises(NotCompilable):
        layer.compile(SemanticQuery(mappable=False))


@needs_shop
def test_compile_escapes_filter_values(layer):
    q = SemanticQuery(
        mappable=True,
        metric="order_count",
        filters=[DimFilter(dimension="city", values=["x' OR '1'='1"])],
    )
    assert rows(layer.compile(q)) == {(0,)}


# ---------- 与行列级权限兼容：受限角色不能通过指标绕过 ----------


def test_compile_refuses_restricted_column(layer):
    q = SemanticQuery(mappable=True, metric="gross_profit", group_by=["brand"])
    for role in ("ops", "sh_manager"):
        with pytest.raises(rls.AccessDenied, match="products.cost"):
            layer.compile(q, role=role)
    # 分析师不受限
    assert "products.cost" in layer.compile(q, role="analyst")


@needs_shop
def test_compiled_sql_still_goes_through_rewrite(layer):
    # 就算绕过 compile 的检查，执行层仍按角色改写：区域经理只能看到上海
    q = SemanticQuery(mappable=True, metric="buyers", group_by=["city"])
    got = rows(layer.compile(q), role="sh_manager")
    assert {r[0] for r in got} == {"上海"}
    # 直接把指标表达式拼进 SQL 也读不到成本
    with pytest.raises(SQLRejected):
        run_readonly(
            SHOP,
            "SELECT SUM(orders.amount - products.cost * orders.qty) FROM orders "
            "JOIN products ON orders.sku = products.sku",
            policy=rls.policy_for("shop_ops", "ops"),
        )


# ---------- 流程接入 ----------

E4 = FixedPipeline(PipelineConfig(schema="enhanced", value_check=True, max_corrections=3))


@needs_shop
def test_context_mode_puts_definitions_in_prompt():
    tools._tables_cached.cache_clear()
    llm = ScriptedLLM([SQLAnswer(sql="SELECT 1")])
    out = asyncio.run(SemanticPipeline(E4, "context")(llm, SHOP, "2025 年各品类的销售额", ""))
    user = llm.calls[0]["messages"][0]["content"]
    assert "SUM(orders.amount)" in user and out.sql == "SELECT 1"
    assert out.trace[0]["step"] == "semantic" and out.trace[0]["matched"] == ["sales"]


@needs_shop
def test_compile_mode_uses_semantic_sql_with_one_call():
    tools._tables_cached.cache_clear()
    plan = SemanticQuery(mappable=True, metric="items_sold")
    llm = ScriptedLLM([plan])
    out = asyncio.run(SemanticPipeline(E4, "compile")(llm, SHOP, "一共卖出了多少件", ""))
    assert "SUM(orders.qty)" in out.sql and len(out.llm_calls) == 1
    assert out.rows and out.exec_error == ""
    assert any(t["step"] == "plan" and t["compiled"] for t in out.trace)


@needs_shop
def test_compile_mode_falls_back_to_context_when_unmappable():
    tools._tables_cached.cache_clear()
    llm = ScriptedLLM([SemanticQuery(mappable=False), SQLAnswer(sql="SELECT 2")])
    out = asyncio.run(SemanticPipeline(E4, "compile")(llm, SHOP, "2025 年的复购率", ""))
    assert out.sql == "SELECT 2" and len(out.llm_calls) == 2
    assert "复购率" in llm.calls[1]["messages"][0]["content"]


@needs_shop
def test_compile_mode_restricted_metric_returns_no_permission():
    tools._tables_cached.cache_clear()
    plan = SemanticQuery(mappable=True, metric="gross_profit", group_by=["brand"])
    out = asyncio.run(
        SemanticPipeline(E4, "compile")(ScriptedLLM([plan]), SHOP, "各品牌毛利", "", "ops")
    )
    assert out.sql.startswith("SELECT 'NO_PERMISSION: products.cost'")
    assert out.rows == [("NO_PERMISSION: products.cost",)]


def test_db_without_layer_passes_through(shop_db_dir):
    llm = ScriptedLLM([SQLAnswer(sql="SELECT 1")])
    out = asyncio.run(SemanticPipeline(E4, "context")(llm, config.db_path("shop"), "q", "ev"))
    assert llm.calls[0]["messages"][0]["content"] == "-- Question: q\n-- External Knowledge: ev"
    assert out.sql == "SELECT 1"
