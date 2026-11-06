"""数据质量检查：每类检查在小库上能报出问题、干净时通过；命令行；缺陷注入评测；接口权限。"""

import json
import sqlite3
from datetime import date

import httpx
import pytest

from datapilot import config, tools
from datapilot.quality import QualityChecker, main, render, report

SHOP = config.DATA_DIR / "dev_databases" / "shop_ops" / "shop_ops.sqlite"
needs_shop = pytest.mark.skipif(
    not SHOP.exists(), reason="先运行 scripts/gen_shop_ops.py 生成电商库"
)

RULES = {
    "unique": [{"columns": ["orders.user_id", "orders.created_at"], "severity": "warn"}],
    "not_null": [{"column": "users.city", "severity": "warn"}],
    "accepted_values": [{"column": "orders.status", "values": ["paid", "cancelled"]}],
    "ranges": [
        {"column": "orders.amount", "min": 0},
        {"column": "orders.rate", "min": 0, "max": 1},
    ],
    "expressions": [
        {
            "id": "paid_has_time",
            "table": "orders",
            "description": "已支付有支付时间",
            "condition": "status != 'paid' OR paid_at IS NOT NULL",
        }
    ],
    "freshness": [{"column": "orders.created_at", "max_age_days": 3}],
}


@pytest.fixture
def mini(tmp_path):
    db = tmp_path / "mini.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (user_id TEXT PRIMARY KEY, city TEXT);
        CREATE TABLE orders (order_id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(user_id),
                             status TEXT, amount REAL, rate REAL, created_at TEXT, paid_at TEXT);
        INSERT INTO users VALUES ('U1', '上海'), ('U2', '北京');
        INSERT INTO orders VALUES
          ('O1', 'U1', 'paid', 10, 0.1, '2026-01-01 10:00:00', '2026-01-01 10:01:00'),
          ('O2', 'U2', 'cancelled', 20, 0.2, '2026-01-02 10:00:00', NULL);
        """
    )
    conn.commit()
    conn.close()
    return db


def run(db, as_of=date(2026, 1, 3)):
    c = QualityChecker(db, RULES)
    try:
        return {ch.id: ch for ch in c.run(as_of)}
    finally:
        c.close()


def test_clean_db_all_pass(mini):
    checks = run(mini)
    assert all(c.status == "pass" for c in checks.values()), [
        c.id for c in checks.values() if c.status != "pass"
    ]
    # 自动生成：主键、外键
    assert {"pk_unique:users", "pk_not_null:orders", "fk:orders.user_id"} <= set(checks)


@pytest.mark.parametrize(
    "sql, check_id, status",
    [
        ("INSERT INTO users VALUES (NULL, '广州')", "pk_not_null:users", "fail"),
        (
            "INSERT INTO orders VALUES ('O3', 'U1', 'paid', 1, 0, '2026-01-01 10:00:00', 'x')",
            "unique:orders.user_id+orders.created_at",
            "warn",
        ),
        ("UPDATE users SET city = '' WHERE user_id = 'U2'", "not_null:users.city", "warn"),
        ("UPDATE orders SET status = 'lost' WHERE order_id = 'O1'", "values:orders.status", "fail"),
        ("UPDATE orders SET amount = -5 WHERE order_id = 'O1'", "range:orders.amount", "fail"),
        ("UPDATE orders SET rate = 1.2 WHERE order_id = 'O2'", "range:orders.rate", "fail"),
        ("UPDATE orders SET user_id = 'U9' WHERE order_id = 'O2'", "fk:orders.user_id", "fail"),
        ("UPDATE orders SET paid_at = NULL WHERE order_id = 'O1'", "rule:paid_has_time", "fail"),
    ],
)
def test_each_check_catches_its_defect(mini, sql, check_id, status):
    conn = sqlite3.connect(mini)
    conn.execute(sql)
    conn.commit()
    conn.close()
    ch = run(mini)[check_id]
    assert ch.status == status and ch.failed >= 1 and ch.sample_keys


def test_sample_keys_are_primary_keys_only(mini):
    conn = sqlite3.connect(mini)
    conn.execute("UPDATE orders SET amount = -5 WHERE order_id = 'O1'")
    conn.commit()
    conn.close()
    assert run(mini)["range:orders.amount"].sample_keys == ["O1"]


def test_freshness(mini):
    assert run(mini, date(2026, 1, 4))["freshness:orders.created_at"].status == "pass"
    stale = run(mini, date(2026, 2, 1))["freshness:orders.created_at"]
    assert stale.status == "warn" and "30 天" in stale.detail


def test_rules_with_unknown_column_rejected(mini):
    c = QualityChecker(mini, {"not_null": [{"column": "users.nope"}]})
    with pytest.raises(ValueError, match="users.nope"):
        c.run(date(2026, 1, 3))
    c.close()


def test_rules_cannot_inject_identifiers(mini):
    c = QualityChecker(mini, {"not_null": [{"column": 'users.city" OR 1=1 --'}]})
    with pytest.raises(ValueError):
        c.run(date(2026, 1, 3))
    c.close()


@needs_shop
def test_shop_ops_report_and_cli(capsys):
    rep = report("shop_ops", date(2026, 10, 6))
    assert rep["summary"]["fail"] == 0 and rep["summary"]["warn"] == 0
    assert {c["category"] for c in rep["checks"]} == {
        "空值",
        "重复",
        "取值范围",
        "外键一致性",
        "业务规则",
        "新鲜度",
    }
    assert "数据质量" in render(rep)
    assert main(["shop_ops", "--as-of", "2026-10-06", "--json", "--strict"]) == 0
    assert json.loads(capsys.readouterr().out)["db_id"] == "shop_ops"
    assert main(["no_such_db"]) == 2


@needs_shop
def test_injected_defects_all_detected():
    from evals.quality_eval import evaluate

    out = evaluate(SHOP)
    assert out["clean_false_positives"] == []
    assert out["detected"] == out["injected"] >= 13


@needs_shop
async def test_api_quality_and_metrics_respect_roles():
    from datapilot.api import create_app
    from datapilot.service import ChatService
    from datapilot.store import MemoryStore

    tools._tables_cached.cache_clear()
    app = create_app(service=ChatService(MemoryStore(), llm_factory=lambda: None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        q = await c.get("/api/databases/shop_ops/quality", params={"as_of": "2026-10-06"})
        assert q.status_code == 200 and q.json()["summary"]["fail"] == 0
        assert (
            await c.get("/api/databases/shop_ops/quality", headers={"X-Role": "ops"})
        ).status_code == 403
        assert (
            await c.get("/api/databases/shop_ops/quality", params={"as_of": "bad"})
        ).status_code == 400

        m = (await c.get("/api/databases/shop_ops/metrics")).json()
        gp = next(x for x in m["metrics"] if x["id"] == "gross_profit")
        assert "products.cost" in gp["lineage"] and gp["restricted"] == []
        assert "brand" in gp["dimensions"]
        m_ops = (await c.get("/api/databases/shop_ops/metrics", headers={"X-Role": "ops"})).json()
        gp = next(x for x in m_ops["metrics"] if x["id"] == "gross_profit")
        assert gp["restricted"] == ["products.cost"]
        assert (await c.get("/api/databases/california_schools/metrics")).status_code == 404
        bad = await c.get("/api/databases/shop_ops/metrics", headers={"X-Role": "ceo"})
        assert bad.status_code == 403
