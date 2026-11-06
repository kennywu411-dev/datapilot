# ruff: noqa: E501  （题目数据，中文问法和 SQL 不折行便于对照）
"""语义层留出题：指标字典定稿之后再写的新问法，用来检验字典是否只拟合了原来 40 题。

- 问法刻意换成字典里没有、或只部分出现的说法（成交额、卖得最好、回购率、处理完的售后单……）；
- 不带 evidence，标准答案按指标字典里的公司口径写；
- 题目确认前不跑付费评测，跑过之后不再为这批题改字典。
运行后写出 questions_holdout.json 和 metric_labels_holdout.json。
"""

import json
from pathlib import Path

VALID = "status NOT IN ('cancelled', 'pending')"
Q2 = "(CAST(substr({c}, 6, 2) AS INTEGER) + 2) / 3"

# (题号, 难度, 问题, 标准 SQL, 应匹配的指标 / 术语)
Q = [
    (
        101,
        "simple",
        "2026 年 3 月的成交额是多少？",
        f"SELECT SUM(amount) FROM orders WHERE created_at LIKE '2026-03%' AND {VALID}",
        ["sales"],
    ),
    (
        102,
        "simple",
        "2025 年每个下单渠道的 GMV 分别是多少？",
        f"SELECT channel, SUM(amount) FROM orders WHERE created_at LIKE '2025%' AND {VALID} GROUP BY channel",
        ["sales"],
    ),
    (
        103,
        "moderate",
        "2025 年哪个品牌卖得最好？按营业额算，只给出品牌。",
        f"SELECT p.brand FROM orders o JOIN products p ON o.sku = p.sku WHERE o.created_at LIKE '2025%' "
        f"AND o.{VALID} GROUP BY p.brand ORDER BY SUM(o.amount) DESC LIMIT 1",
        ["sales"],
    ),
    (
        104,
        "simple",
        "2026 年上半年平均每单实付多少钱？保留两位小数。",
        f"SELECT ROUND(AVG(amount), 2) FROM orders WHERE created_at BETWEEN '2026-01-01' AND '2026-06-30 23:59:59' AND {VALID}",
        ["aov"],
    ),
    (
        105,
        "moderate",
        "2025 年男性用户的客单价是多少？保留两位小数。",
        f"SELECT ROUND(AVG(o.amount), 2) FROM orders o JOIN users u ON o.user_id = u.user_id "
        f"WHERE u.gender = 'M' AND o.created_at LIKE '2025%' AND o.{VALID}",
        ["aov"],
    ),
    (
        106,
        "moderate",
        "2025 年家居品类一共卖出去多少件？",
        f"SELECT SUM(o.qty) FROM orders o JOIN products p ON o.sku = p.sku "
        f"WHERE p.category = 'home' AND o.created_at LIKE '2025%' AND o.{VALID}",
        ["items_sold"],
    ),
    (
        107,
        "simple",
        "2026 年 1 到 9 月有多少活跃用户？",
        f"SELECT COUNT(DISTINCT user_id) FROM orders WHERE created_at BETWEEN '2026-01-01' AND '2026-09-30 23:59:59' AND {VALID}",
        ["buyers"],
    ),
    (
        108,
        "simple",
        "2025 年在 App 上买过东西的人有多少？",
        f"SELECT COUNT(DISTINCT user_id) FROM orders WHERE channel = 'app' AND created_at LIKE '2025%' AND {VALID}",
        ["buyers"],
    ),
    (
        109,
        "challenging",
        "2025 年的回购率是多少？用百分比表示，保留两位小数。",
        f"SELECT ROUND(100.0 * SUM(n >= 2) / COUNT(*), 2) FROM (SELECT user_id, COUNT(*) AS n FROM orders "
        f"WHERE created_at LIKE '2025%' AND {VALID} GROUP BY user_id)",
        ["repurchase_rate"],
    ),
    (
        110,
        "moderate",
        "2026 年 1–9 月的退款率是多少？用百分比表示，保留两位小数。",
        "SELECT ROUND(100.0 * SUM(status = 'refunded') / COUNT(*), 2) FROM orders "
        "WHERE paid_at IS NOT NULL AND created_at LIKE '2026%'",
        ["refund_rate"],
    ),
    (
        111,
        "moderate",
        "2025 年各品类的毛利润分别是多少？保留两位小数。",
        "SELECT p.category, ROUND(SUM(o.amount - p.cost * o.qty), 2) FROM orders o JOIN products p ON o.sku = p.sku "
        "WHERE o.created_at LIKE '2025%' AND o.status IN ('paid', 'shipped', 'delivered') GROUP BY p.category",
        ["gross_profit"],
    ),
    (
        112,
        "challenging",
        "2025 年美妆品类的售后申请率是多少？用百分比表示，保留一位小数。",
        "SELECT ROUND(100.0 * COUNT(DISTINCT t.order_id) / COUNT(DISTINCT o.order_id), 1) FROM orders o "
        "JOIN products p ON o.sku = p.sku LEFT JOIN tickets t ON t.order_id = o.order_id "
        "WHERE p.category = 'beauty' AND o.status IN ('delivered', 'refunded') AND o.created_at LIKE '2025%'",
        ["after_sales_rate"],
    ),
    (
        113,
        "simple",
        "因为质量问题的售后，一共退款多少钱？",
        "SELECT SUM(refund_amount) FROM tickets WHERE reason = 'quality'",
        ["refund_amount"],
    ),
    (
        114,
        "challenging",
        "2025 年每个季度的净销售额是多少？给出季度（1–4）和金额，保留两位小数。",
        f"SELECT s.q, ROUND(s.sales - COALESCE(r.refund, 0), 2) FROM "
        f"(SELECT {Q2.format(c='created_at')} AS q, SUM(amount) AS sales FROM orders "
        f"WHERE created_at LIKE '2025%' AND {VALID} GROUP BY 1) s LEFT JOIN "
        f"(SELECT {Q2.format(c='o.created_at')} AS q, SUM(t.refund_amount) AS refund FROM tickets t "
        f"JOIN orders o ON t.order_id = o.order_id WHERE o.created_at LIKE '2025%' GROUP BY 1) r ON s.q = r.q",
        ["net_sales"],
    ),
    (
        115,
        "challenging",
        "2026 年 6 月的 GMV 环比增长了多少？用百分比表示，保留两位小数。",
        f"SELECT ROUND(100.0 * (SUM(CASE WHEN created_at LIKE '2026-06%' THEN amount END) "
        f"- SUM(CASE WHEN created_at LIKE '2026-05%' THEN amount END)) "
        f"/ SUM(CASE WHEN created_at LIKE '2026-05%' THEN amount END), 2) FROM orders WHERE {VALID}",
        ["sales"],
    ),
    (
        116,
        "simple",
        "2025 年小程序渠道的有效单量有多少？",
        f"SELECT COUNT(*) FROM orders WHERE channel = 'mini_program' AND created_at LIKE '2025%' AND {VALID}",
        ["valid_order_count"],
    ),
    (
        117,
        "moderate",
        "2025 年申请、已经处理完的售后单，平均花了几天？保留一位小数。",
        "SELECT ROUND(AVG(julianday(closed_at) - julianday(created_at)), 1) FROM tickets "
        "WHERE status = 'closed' AND created_at LIKE '2025%'",
        ["resolve_days", "ticket_closed"],
    ),
    (
        118,
        "simple",
        "2025 年新增了多少注册用户？",
        "SELECT COUNT(*) FROM users WHERE register_date LIKE '2025%'",
        ["new_users"],
    ),
    (
        119,
        "simple",
        "标价超过 500 元的商品有几种？",
        "SELECT COUNT(*) FROM products WHERE price > 500",
        [],
    ),
]

here = Path(__file__).parent
(here / "questions_holdout.json").write_text(
    json.dumps(
        [
            {
                "question_id": i,
                "db_id": "shop_ops",
                "question": q,
                "evidence": "",
                "SQL": sql,
                "difficulty": d,
            }
            for i, d, q, sql, _ in Q
        ],
        ensure_ascii=False,
        indent=1,
    ),
    encoding="utf-8",
)
(here / "metric_labels_holdout.json").write_text(
    json.dumps({str(i): labels for i, *_, labels in Q}, ensure_ascii=False, indent=1),
    encoding="utf-8",
)
print(len(Q), "题")
