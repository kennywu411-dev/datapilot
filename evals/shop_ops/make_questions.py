# ruff: noqa: E501  （题目数据，中文问法和 SQL 不折行便于对照）
"""电商经营分析评测题（构造题，标准 SQL 逐题核对）。运行后写出 questions.json。

口径约定（写进需要的题的 evidence）：
- 有效订单：status 不是 cancelled、pending
- 销售额：有效订单的实付金额 amount 之和
"""

import json
from pathlib import Path

VALID = "status NOT IN ('cancelled', 'pending')"
EV_SALES = (
    "有效订单指 status 不是 cancelled 和 pending 的订单；销售额指有效订单的实付金额 amount 之和"
)

Q = [
    # ---------- simple ----------
    ("simple", "一共有多少位超级会员？", "", "SELECT COUNT(*) FROM users WHERE tier = 'svip'"),
    (
        "simple",
        "数码品类有多少种商品？",
        "",
        "SELECT COUNT(*) FROM products WHERE category = 'digital'",
    ),
    (
        "simple",
        "2025 年一共有多少单被取消？",
        "",
        "SELECT COUNT(*) FROM orders WHERE status = 'cancelled' AND created_at LIKE '2025%'",
    ),
    (
        "simple",
        "来自成都的女性用户有多少人？",
        "",
        "SELECT COUNT(*) FROM users WHERE city = '成都' AND gender = 'F'",
    ),
    (
        "simple",
        "标价最高的商品是什么？给出名称和标价。",
        "",
        "SELECT name, price FROM products ORDER BY price DESC LIMIT 1",
    ),
    (
        "simple",
        "2026 年 618 活动的折扣率是多少？",
        "",
        "SELECT discount_rate FROM campaigns WHERE name = '2026年618'",
    ),
    (
        "simple",
        "因为发错货申请的售后工单有多少张？",
        "",
        "SELECT COUNT(*) FROM tickets WHERE reason = 'wrong_item'",
    ),
    (
        "simple",
        "小程序渠道一共下了多少单？",
        "",
        "SELECT COUNT(*) FROM orders WHERE channel = 'mini_program'",
    ),
    (
        "simple",
        "还在处理中的换货工单有几张？",
        "",
        "SELECT COUNT(*) FROM tickets WHERE type = 'exchange' AND status = 'open'",
    ),
    (
        "simple",
        "2025 年 9 月注册的用户有多少？",
        "",
        "SELECT COUNT(*) FROM users WHERE register_date LIKE '2025-09%'",
    ),
    # ---------- moderate ----------
    (
        "moderate",
        "2025 年各品类的销售额分别是多少？",
        EV_SALES,
        f"SELECT p.category, SUM(o.amount) FROM orders o JOIN products p ON o.sku = p.sku "
        f"WHERE o.created_at LIKE '2025%' AND o.{VALID} GROUP BY p.category",
    ),
    (
        "moderate",
        "2025 年哪个月的订单量最多？给出月份（YYYY-MM）和订单数。",
        "订单量按下单数计，包含所有状态",
        "SELECT substr(created_at, 1, 7), COUNT(*) FROM orders WHERE created_at LIKE '2025%' "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
    ),
    (
        "moderate",
        "售后工单最多的前 3 个品类是哪些？给出品类和工单数。",
        "",
        "SELECT p.category, COUNT(*) FROM tickets t JOIN orders o ON t.order_id = o.order_id "
        "JOIN products p ON o.sku = p.sku GROUP BY p.category ORDER BY 2 DESC LIMIT 3",
    ),
    (
        "moderate",
        "各会员等级的客单价是多少？保留两位小数。",
        "客单价 = 有效订单的实付金额之和 ÷ 有效订单数；有效订单指 status 不是 cancelled 和 pending",
        f"SELECT u.tier, ROUND(AVG(o.amount), 2) FROM orders o JOIN users u ON o.user_id = u.user_id "
        f"WHERE o.{VALID} GROUP BY u.tier",
    ),
    (
        "moderate",
        "2025 年的退款率是多少？用百分比表示，保留两位小数。",
        "退款率 = status 为 refunded 的订单数 ÷ 已支付订单数 × 100；已支付订单指 paid_at 不为空的订单",
        "SELECT ROUND(100.0 * SUM(status = 'refunded') / COUNT(*), 2) FROM orders "
        "WHERE paid_at IS NOT NULL AND created_at LIKE '2025%'",
    ),
    (
        "moderate",
        "2026 年参加了活动的订单占有效订单的百分之多少？保留两位小数。",
        "有效订单指 status 不是 cancelled 和 pending 的订单",
        f"SELECT ROUND(100.0 * SUM(campaign_id IS NOT NULL) / COUNT(*), 2) FROM orders "
        f"WHERE created_at LIKE '2026%' AND {VALID}",
    ),
    (
        "moderate",
        "2025 年哪个城市的用户在美妆上花钱最多？给出城市和金额。",
        EV_SALES,
        f"SELECT u.city, SUM(o.amount) FROM orders o JOIN users u ON o.user_id = u.user_id "
        f"JOIN products p ON o.sku = p.sku WHERE p.category = 'beauty' AND o.created_at LIKE '2025%' "
        f"AND o.{VALID} GROUP BY u.city ORDER BY 2 DESC LIMIT 1",
    ),
    (
        "moderate",
        "2025 年双 11 大促一共卖出了多少件商品？",
        "只算有效订单（status 不是 cancelled 和 pending）",
        f"SELECT SUM(o.qty) FROM orders o JOIN campaigns c ON o.campaign_id = c.campaign_id "
        f"WHERE c.name = '2025双11大促' AND o.{VALID}",
    ),
    (
        "moderate",
        "被拒绝的售后工单里，最常见的原因是什么？",
        "",
        "SELECT reason FROM tickets WHERE status = 'rejected' GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 1",
    ),
    (
        "moderate",
        "买过降噪耳机的用户有多少人？",
        "只算有效订单（status 不是 cancelled 和 pending）",
        f"SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN products p ON o.sku = p.sku "
        f"WHERE p.name = '降噪耳机' AND o.{VALID}",
    ),
    (
        "moderate",
        "2026 年各下单渠道的订单数占比是多少？用百分比表示，保留一位小数。",
        "订单数包含所有状态",
        "SELECT channel, ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM orders WHERE created_at LIKE '2026%'), 1) "
        "FROM orders WHERE created_at LIKE '2026%' GROUP BY channel",
    ),
    (
        "moderate",
        "已完结的售后工单，从申请到完结平均用了多少天？保留一位小数。",
        "已完结指 status = 'closed'；天数按申请时间和完结时间之差计算，可以带小数",
        "SELECT ROUND(AVG(julianday(closed_at) - julianday(created_at)), 1) FROM tickets WHERE status = 'closed'",
    ),
    (
        "moderate",
        "2025 年毛利最高的品牌是哪个？",
        "毛利 = 实付金额 − 进货成本 × 件数；只算 status 为 paid、shipped、delivered 的订单（已退款的不算）",
        "SELECT p.brand FROM orders o JOIN products p ON o.sku = p.sku "
        "WHERE o.created_at LIKE '2025%' AND o.status IN ('paid', 'shipped', 'delivered') "
        "GROUP BY p.brand ORDER BY SUM(o.amount - p.cost * o.qty) DESC LIMIT 1",
    ),
    (
        "moderate",
        "下单后超过 5 天才签收的订单有多少单？",
        "按下单时间和签收时间之差计算，超过 5 天指大于 5 天",
        "SELECT COUNT(*) FROM orders WHERE delivered_at IS NOT NULL "
        "AND julianday(delivered_at) - julianday(created_at) > 5",
    ),
    (
        "moderate",
        "从来没有下过单的用户有多少人？",
        "",
        "SELECT COUNT(*) FROM users WHERE user_id NOT IN (SELECT user_id FROM orders)",
    ),
    (
        "moderate",
        "2024 年 10 月到 12 月，每个月新注册了多少用户？",
        "",
        "SELECT substr(register_date, 1, 7), COUNT(*) FROM users "
        "WHERE register_date BETWEEN '2024-10-01' AND '2024-12-31' GROUP BY 1",
    ),
    (
        "moderate",
        "退货退款类的售后工单，一共退了多少钱？",
        "",
        "SELECT SUM(refund_amount) FROM tickets WHERE type = 'return'",
    ),
    (
        "moderate",
        "2025 年下单次数最多的 VIP 会员是谁？给出姓名和订单数。",
        "订单数包含所有状态",
        "SELECT u.name, COUNT(*) FROM orders o JOIN users u ON o.user_id = u.user_id "
        "WHERE u.tier = 'vip' AND o.created_at LIKE '2025%' GROUP BY u.user_id ORDER BY 2 DESC LIMIT 1",
    ),
    # ---------- challenging ----------
    (
        "challenging",
        "2026 年 9 月的销售额环比变化了百分之多少？下降用负数表示，保留两位小数。",
        EV_SALES + "；按下单时间归属月份",
        f"SELECT ROUND(100.0 * (SUM(CASE WHEN created_at LIKE '2026-09%' THEN amount END) "
        f"- SUM(CASE WHEN created_at LIKE '2026-08%' THEN amount END)) "
        f"/ SUM(CASE WHEN created_at LIKE '2026-08%' THEN amount END), 2) FROM orders WHERE {VALID}",
    ),
    (
        "challenging",
        "2026 年 1–9 月的销售额，同比增长了百分之多少？保留两位小数。",
        EV_SALES,
        f"SELECT ROUND(100.0 * (SUM(CASE WHEN created_at BETWEEN '2026-01-01' AND '2026-09-30 23:59:59' THEN amount END) "
        f"- SUM(CASE WHEN created_at BETWEEN '2025-01-01' AND '2025-09-30 23:59:59' THEN amount END)) "
        f"/ SUM(CASE WHEN created_at BETWEEN '2025-01-01' AND '2025-09-30 23:59:59' THEN amount END), 2) "
        f"FROM orders WHERE {VALID}",
    ),
    (
        "challenging",
        "2026 年每个月数码品类的售后率是多少？用百分比表示，保留一位小数。",
        "售后率 = 申请过售后的已签收订单数 ÷ 已签收订单数 × 100；已签收订单指 status 为 delivered 或 refunded；按下单时间归属月份",
        "SELECT substr(o.created_at, 1, 7), ROUND(100.0 * COUNT(DISTINCT t.order_id) / COUNT(DISTINCT o.order_id), 1) "
        "FROM orders o JOIN products p ON o.sku = p.sku LEFT JOIN tickets t ON t.order_id = o.order_id "
        "WHERE p.category = 'digital' AND o.status IN ('delivered', 'refunded') AND o.created_at LIKE '2026%' "
        "GROUP BY 1",
    ),
    (
        "challenging",
        "2025 年的复购率是多少？用百分比表示，保留两位小数。",
        "复购率 = 2025 年下过 2 次及以上有效订单的用户数 ÷ 2025 年下过有效订单的用户数 × 100；有效订单指 status 不是 cancelled 和 pending",
        f"SELECT ROUND(100.0 * SUM(n >= 2) / COUNT(*), 2) FROM (SELECT user_id, COUNT(*) AS n FROM orders "
        f"WHERE created_at LIKE '2025%' AND {VALID} GROUP BY user_id)",
    ),
    (
        "challenging",
        "2025 年每个品类里，销售额最高的是哪个商品？给出品类和商品名称。",
        EV_SALES,
        f"SELECT category, name FROM (SELECT p.category, p.name, RANK() OVER (PARTITION BY p.category "
        f"ORDER BY SUM(o.amount) DESC) AS rk FROM orders o JOIN products p ON o.sku = p.sku "
        f"WHERE o.created_at LIKE '2025%' AND o.{VALID} GROUP BY p.sku) WHERE rk = 1",
    ),
    (
        "challenging",
        "2025 年双 11 大促和 2026 年 618 都下过单的用户有多少人？",
        "只算有效订单（status 不是 cancelled 和 pending），且订单参加了对应活动",
        f"SELECT COUNT(*) FROM (SELECT o.user_id FROM orders o JOIN campaigns c ON o.campaign_id = c.campaign_id "
        f"WHERE c.name = '2025双11大促' AND o.{VALID} INTERSECT SELECT o.user_id FROM orders o "
        f"JOIN campaigns c ON o.campaign_id = c.campaign_id WHERE c.name = '2026年618' AND o.{VALID})",
    ),
    (
        "challenging",
        "2025 年申请过售后的用户里，超级会员占百分之多少？保留两位小数。",
        "按申请时间算 2025 年；用户按人数去重",
        "SELECT ROUND(100.0 * COUNT(DISTINCT CASE WHEN u.tier = 'svip' THEN u.user_id END) / COUNT(DISTINCT u.user_id), 2) "
        "FROM tickets t JOIN orders o ON t.order_id = o.order_id JOIN users u ON o.user_id = u.user_id "
        "WHERE t.created_at LIKE '2025%'",
    ),
    (
        "challenging",
        "把 2025 年的有效订单按实付金额分成 200 元以下、200–500 元、500–1000 元、1000 元及以上四档，每档各有多少单？只需给出各档的订单数。",
        "有效订单指 status 不是 cancelled 和 pending；各档左闭右开，如 200–500 元指 200 ≤ 金额 < 500",
        f"SELECT COUNT(*) FROM orders WHERE created_at LIKE '2025%' AND {VALID} "
        f"GROUP BY CASE WHEN amount < 200 THEN 1 WHEN amount < 500 THEN 2 WHEN amount < 1000 THEN 3 ELSE 4 END",
    ),
    (
        "challenging",
        "首单在 2025 年的新客，首单平均实付多少钱？保留两位小数。",
        "首单指用户最早的一笔有效订单；有效订单指 status 不是 cancelled 和 pending",
        f"SELECT ROUND(AVG(amount), 2) FROM (SELECT amount, created_at, ROW_NUMBER() OVER (PARTITION BY user_id "
        f"ORDER BY created_at) AS rn FROM orders WHERE {VALID}) WHERE rn = 1 AND created_at LIKE '2025%'",
    ),
    (
        "challenging",
        "2025 年的有效订单里，参加活动的平均实付金额比没参加活动的低多少元？保留两位小数。",
        "有效订单指 status 不是 cancelled 和 pending；用没参加活动的平均值减去参加活动的平均值",
        f"SELECT ROUND(AVG(CASE WHEN campaign_id IS NULL THEN amount END) "
        f"- AVG(CASE WHEN campaign_id IS NOT NULL THEN amount END), 2) FROM orders "
        f"WHERE created_at LIKE '2025%' AND {VALID}",
    ),
    (
        "challenging",
        "2025 年每个季度，哪个城市的用户贡献的销售额最高？给出季度（1–4）和城市。",
        EV_SALES,
        f"SELECT q, city FROM (SELECT (CAST(substr(o.created_at, 6, 2) AS INTEGER) + 2) / 3 AS q, u.city, "
        f"RANK() OVER (PARTITION BY (CAST(substr(o.created_at, 6, 2) AS INTEGER) + 2) / 3 ORDER BY SUM(o.amount) DESC) AS rk "
        f"FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.created_at LIKE '2025%' AND o.{VALID} "
        f"GROUP BY 1, 2) WHERE rk = 1",
    ),
    (
        "challenging",
        "2025 年哪个月的退款金额占当月销售额的比例最高？给出月份（YYYY-MM）。",
        EV_SALES + "；退款金额指售后工单的 refund_amount 之和；两者都按订单的下单时间归属月份",
        f"SELECT m FROM (SELECT substr(o.created_at, 1, 7) AS m, SUM(o.amount) AS sales, "
        f"(SELECT SUM(t.refund_amount) FROM tickets t JOIN orders o2 ON t.order_id = o2.order_id "
        f"WHERE substr(o2.created_at, 1, 7) = substr(o.created_at, 1, 7)) AS refund "
        f"FROM orders o WHERE o.created_at LIKE '2025%' AND o.{VALID} GROUP BY 1) ORDER BY refund / sales DESC LIMIT 1",
    ),
]

out = [
    {
        "question_id": i,
        "db_id": "shop_ops",
        "question": q,
        "evidence": ev,
        "SQL": sql,
        "difficulty": d,
    }
    for i, (d, q, ev, sql) in enumerate(Q, 1)
]
Path(__file__).with_name("questions.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
)
print(len(out), "题")
