"""数据质量检查的缺陷注入评测（离线，不调用模型）。

在电商库的副本上逐个注入一类已知缺陷，检查对应规则是否报出来；另外在干净库上看有没有误报。

  uv run python evals/quality_eval.py            # 打印结果
  uv run python evals/quality_eval.py --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datapilot import config  # noqa: E402
from datapilot.quality import QualityChecker, load_rules  # noqa: E402

# 干净库的检查日期取数据截止后几天，新鲜度应通过
CLEAN_AS_OF = date(2026, 10, 6)

# (缺陷, 注入 SQL, 应报出的检查, 检查日期)
INJECTIONS = [
    (
        "主键为空",
        "INSERT INTO products VALUES (NULL, '测试', 'home', '测试', 10, 5)",
        "pk_not_null:products",
        CLEAN_AS_OF,
    ),
    (
        "重复导入订单",
        "INSERT INTO orders SELECT 'DUP' || order_id, user_id, sku, qty, amount, "
        "campaign_id, channel, status, created_at, paid_at, delivered_at FROM orders LIMIT 1",
        "unique:orders.user_id+orders.sku+orders.created_at",
        CLEAN_AS_OF,
    ),
    (
        "城市缺失",
        "UPDATE users SET city = NULL WHERE rowid = 1",
        "not_null:users.city",
        CLEAN_AS_OF,
    ),
    (
        "未知订单状态",
        "UPDATE orders SET status = 'lost' WHERE rowid = 1",
        "values:orders.status",
        CLEAN_AS_OF,
    ),
    (
        "实付金额为负",
        "UPDATE orders SET amount = -1 WHERE rowid = 1",
        "range:orders.amount",
        CLEAN_AS_OF,
    ),
    (
        "折扣率越界",
        "UPDATE campaigns SET discount_rate = 1.5 WHERE rowid = 1",
        "range:campaigns.discount_rate",
        CLEAN_AS_OF,
    ),
    (
        "订单用户不存在",
        "UPDATE orders SET user_id = 'U9999' WHERE rowid = 1",
        "fk:orders.user_id",
        CLEAN_AS_OF,
    ),
    (
        "工单订单不存在",
        "UPDATE tickets SET order_id = 'O9999999' WHERE rowid = 1",
        "fk:tickets.order_id",
        CLEAN_AS_OF,
    ),
    (
        "已签收但无支付时间",
        "UPDATE orders SET paid_at = NULL WHERE status = 'delivered' AND rowid = "
        "(SELECT MIN(rowid) FROM orders WHERE status = 'delivered')",
        "rule:paid_status_has_paid_at",
        CLEAN_AS_OF,
    ),
    (
        "签收早于支付",
        "UPDATE orders SET delivered_at = '2000-01-01 00:00:00' WHERE rowid = "
        "(SELECT MIN(rowid) FROM orders WHERE status = 'delivered')",
        "rule:order_time_sequence",
        CLEAN_AS_OF,
    ),
    (
        "退款超过实付",
        "UPDATE tickets SET refund_amount = 999999 WHERE rowid = 1",
        "rule:refund_not_exceed_amount",
        CLEAN_AS_OF,
    ),
    (
        "已完结工单无完结时间",
        "UPDATE tickets SET closed_at = NULL WHERE rowid = "
        "(SELECT MIN(rowid) FROM tickets WHERE status = 'closed')",
        "rule:closed_ticket_has_closed_at",
        CLEAN_AS_OF,
    ),
    ("数据停更", "", "freshness:orders.created_at", date(2026, 12, 31)),
]


def failing(db: Path, as_of: date) -> set[str]:
    checker = QualityChecker(db, load_rules("shop_ops"))
    try:
        return {c.id for c in checker.run(as_of) if c.status != "pass"}
    finally:
        checker.close()


def evaluate(src: Path | None = None) -> dict:
    src = src or config.db_path("shop_ops")
    clean = failing(src, CLEAN_AS_OF)
    rows = []
    with tempfile.TemporaryDirectory() as d:
        for name, sql, expected, as_of in INJECTIONS:
            db = Path(d) / "shop_ops.sqlite"
            shutil.copy(src, db)
            if sql:
                conn = sqlite3.connect(db)
                conn.execute(sql)
                conn.commit()
                conn.close()
            got = failing(db, as_of)
            rows.append(
                {
                    "defect": name,
                    "expected": expected,
                    "detected": expected in got,
                    "also_flagged": sorted(got - {expected} - clean),
                }
            )
    return {
        "clean_false_positives": sorted(clean),
        "injected": len(rows),
        "detected": sum(r["detected"] for r in rows),
        "rows": rows,
    }


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    out = evaluate()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    print(
        f"注入 {out['injected']} 类缺陷，检出 {out['detected']}；"
        f"干净库误报 {len(out['clean_false_positives'])}：{out['clean_false_positives']}"
    )
    for r in out["rows"]:
        also = f"  连带报出：{r['also_flagged']}" if r["also_flagged"] else ""
        print(f"{'✓' if r['detected'] else '✗'} {r['defect']} → {r['expected']}{also}")


if __name__ == "__main__":
    main()
