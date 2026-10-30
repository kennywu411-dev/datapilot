# ruff: noqa: E501  （DDL 行尾注释必须写在一行，add_database.py 按行提取取值说明）
"""生成模拟电商经营库 shop_ops（固定随机种子，任何人运行都得到同一份数据）。

**数据是模拟的**：用来出有区分度的中文经营分析题，不代表任何真实公司。
为了让趋势题有答案，数据里埋了几条规律：双 11、618 订单高峰，春节低谷；
订单量整体逐月增长；数码品类 2026 年退货率逐月上升；VIP / SVIP 客单价更高；
小程序渠道占比逐年上升。

用法：
  uv run python scripts/gen_shop_ops.py            # 生成到 data/source/shop_ops.sqlite
  uv run python scripts/add_database.py shop_ops data/source/shop_ops.sqlite
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "source" / "shop_ops.sqlite"
SEED = 20261023
START, END = date(2024, 10, 1), date(2026, 9, 30)  # 24 个完整月

DDL = """
CREATE TABLE users (
  user_id TEXT PRIMARY KEY, -- 用户编号，如 U0001
  name TEXT NOT NULL, -- 用户姓名
  gender TEXT, -- F: 女; M: 男
  city TEXT, -- 所在城市（中文，如 上海、北京）
  tier TEXT NOT NULL, -- 会员等级 normal: 普通会员; vip: VIP 会员; svip: 超级会员
  register_date TEXT NOT NULL -- 注册日期 YYYY-MM-DD
);
CREATE TABLE products (
  sku TEXT PRIMARY KEY, -- 商品编号，如 P001
  name TEXT NOT NULL, -- 商品名称（中文）
  category TEXT NOT NULL, -- 品类 apparel: 服饰; digital: 数码; home: 家居; food: 食品; beauty: 美妆
  brand TEXT, -- 品牌
  price REAL NOT NULL, -- 标价，单位元
  cost REAL NOT NULL -- 进货成本，单位元
);
CREATE TABLE campaigns (
  campaign_id INTEGER PRIMARY KEY, -- 活动编号
  name TEXT NOT NULL, -- 活动名称（中文，如 2025双11大促）
  start_date TEXT NOT NULL, -- 开始日期 YYYY-MM-DD
  end_date TEXT NOT NULL, -- 结束日期 YYYY-MM-DD（含当天）
  discount_rate REAL NOT NULL -- 折扣率，0.2 表示减 20%
);
CREATE TABLE orders (
  order_id TEXT PRIMARY KEY, -- 订单号，如 O000001
  user_id TEXT NOT NULL REFERENCES users(user_id), -- 下单用户
  sku TEXT NOT NULL REFERENCES products(sku), -- 购买商品（每单一种商品）
  qty INTEGER NOT NULL, -- 件数
  amount REAL NOT NULL, -- 实付金额，单位元（已扣折扣）
  campaign_id INTEGER REFERENCES campaigns(campaign_id), -- 参与的活动，不参与为 NULL
  channel TEXT NOT NULL, -- 下单渠道 app: App; web: 网页; mini_program: 小程序
  status TEXT NOT NULL, -- pending: 待支付; paid: 已支付待发货; shipped: 已发货; delivered: 已签收; cancelled: 已取消（未支付或支付前取消）; refunded: 已退款
  created_at TEXT NOT NULL, -- 下单时间 YYYY-MM-DD HH:MM:SS
  paid_at TEXT, -- 支付时间，未支付为 NULL
  delivered_at TEXT -- 签收时间，未签收为 NULL
);
CREATE TABLE tickets (
  ticket_id TEXT PRIMARY KEY, -- 售后工单号，如 T00001
  order_id TEXT NOT NULL REFERENCES orders(order_id), -- 关联订单
  type TEXT NOT NULL, -- 售后类型 refund: 仅退款; return: 退货退款; exchange: 换货
  reason TEXT NOT NULL, -- 原因 quality: 质量问题; wrong_item: 发错货; size: 尺码不合适; late: 物流太慢; no_reason: 七天无理由
  status TEXT NOT NULL, -- open: 处理中; approved: 已同意; rejected: 已拒绝; closed: 已完结
  refund_amount REAL NOT NULL, -- 退款金额，单位元；换货或被拒为 0
  created_at TEXT NOT NULL, -- 申请时间 YYYY-MM-DD HH:MM:SS
  closed_at TEXT -- 完结时间，未完结为 NULL
);
"""

SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
GIVEN = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰飞鹏辉宇欣怡晨浩然子轩雨萱"
CITIES = [
    ("上海", 18),
    ("北京", 16),
    ("深圳", 12),
    ("广州", 11),
    ("杭州", 10),
    ("成都", 10),
    ("武汉", 8),
    ("南京", 7),
    ("西安", 5),
    ("重庆", 3),
]
PRODUCTS = {
    "apparel": [
        ("纯棉短袖T恤", "森马", 79),
        ("修身牛仔裤", "Levi's", 399),
        ("羽绒服", "波司登", 899),
        ("运动卫衣", "李宁", 259),
        ("真丝衬衫", "太平鸟", 459),
        ("跑步鞋", "安踏", 369),
    ],
    "digital": [
        ("无线蓝牙耳机", "华为", 699),
        ("智能手表", "小米", 999),
        ("机械键盘", "罗技", 549),
        ("充电宝 20000mAh", "Anker", 199),
        ("平板电脑", "华为", 2699),
        ("降噪耳机", "索尼", 1899),
    ],
    "home": [
        ("乳胶枕", "水星家纺", 199),
        ("空气炸锅", "美的", 349),
        ("四件套床品", "罗莱", 599),
        ("扫地机器人", "科沃斯", 1999),
        ("保温杯", "膳魔师", 189),
    ],
    "food": [
        ("每日坚果礼盒", "三只松鼠", 129),
        ("挂耳咖啡 30 包", "三顿半", 99),
        ("有机大米 5kg", "五常", 89),
        ("牛排套餐", "大希地", 239),
    ],
    "beauty": [
        ("保湿面霜", "珀莱雅", 299),
        ("防晒霜", "安热沙", 198),
        ("口红", "完美日记", 89),
        ("精华液", "雅诗兰黛", 890),
    ],
}
CAMPAIGNS = [
    ("2024双11大促", "2024-11-01", "2024-11-11", 0.2),
    ("2024双12", "2024-12-10", "2024-12-12", 0.1),
    ("2025年货节", "2025-01-10", "2025-01-20", 0.1),
    ("2025年618", "2025-06-01", "2025-06-18", 0.15),
    ("2025双11大促", "2025-11-01", "2025-11-11", 0.2),
    ("2025双12", "2025-12-10", "2025-12-12", 0.1),
    ("2026年货节", "2026-01-15", "2026-01-25", 0.1),
    ("2026年618", "2026-06-01", "2026-06-18", 0.15),
]


def month_index(d: date) -> int:
    return (d.year - START.year) * 12 + d.month - START.month


def daily_weight(d: date) -> float:
    w = 1.0 + 0.025 * month_index(d)  # 逐月增长
    md = (d.month, d.day)
    if (11, 1) <= md <= (11, 11):
        w *= 3.0 if md >= (11, 9) else 1.8
    elif (6, 1) <= md <= (6, 18):
        w *= 1.6
    elif (12, 10) <= md <= (12, 12):
        w *= 1.5
    if d.month == 2 and d.day <= 20:  # 春节低谷
        w *= 0.55
    if d.weekday() >= 5:
        w *= 1.15
    return w


def ts(d: date, rng: random.Random) -> datetime:
    return datetime(
        d.year,
        d.month,
        d.day,
        rng.choices(range(24), weights=HOUR_W)[0],
        rng.randrange(60),
        rng.randrange(60),
    )


HOUR_W = [1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 6, 7, 6, 6, 6, 6, 7, 8, 9, 10, 10, 8, 4]


def main() -> None:
    argparse.ArgumentParser(
        description=f"生成模拟电商经营库 shop_ops 到 {OUT.relative_to(ROOT)}（固定种子，可重复）",
        epilog="接入系统：uv run python scripts/add_database.py shop_ops data/source/shop_ops.sqlite",
    ).parse_args()
    rng = random.Random(SEED)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    conn = sqlite3.connect(OUT)
    conn.executescript(DDL)

    # 用户：600 人，注册日期分布在 2023-01 到 2026-09
    users = []
    for i in range(1, 601):
        tier = rng.choices(["normal", "vip", "svip"], weights=[75, 20, 5])[0]
        reg = date(2023, 1, 1) + timedelta(days=rng.randrange((END - date(2023, 1, 1)).days))
        users.append(
            (
                f"U{i:04d}",
                rng.choice(SURNAMES) + "".join(rng.sample(GIVEN, rng.choice([1, 2]))),
                rng.choice("FFFMM"),
                rng.choices([c for c, _ in CITIES], weights=[w for _, w in CITIES])[0],
                tier,
                reg.isoformat(),
            )
        )
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)", users)

    products, n = [], 0
    for cat, items in PRODUCTS.items():
        for name, brand, price in items:
            n += 1
            margin = {"apparel": 0.45, "digital": 0.2, "home": 0.35, "food": 0.3, "beauty": 0.6}[
                cat
            ]
            products.append(
                (f"P{n:03d}", name, cat, brand, float(price), round(price * (1 - margin), 2))
            )
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", products)
    cat_weight = {"apparel": 30, "digital": 22, "home": 18, "food": 18, "beauty": 12}

    conn.executemany(
        "INSERT INTO campaigns VALUES (?,?,?,?,?)", [(i + 1, *c) for i, c in enumerate(CAMPAIGNS)]
    )
    camp = [
        (i + 1, date.fromisoformat(s), date.fromisoformat(e), r)
        for i, (_, s, e, r) in enumerate(CAMPAIGNS)
    ]

    # 订单：总量约 6000
    days = [START + timedelta(days=k) for k in range((END - START).days + 1)]
    weights = [daily_weight(d) for d in days]
    scale = 6000 / sum(weights)
    orders, tickets, oid, tid = [], [], 0, 0
    for d, w in zip(days, weights, strict=True):
        cnt = int(w * scale) + (1 if rng.random() < (w * scale) % 1 else 0)
        for _ in range(cnt):
            oid += 1
            u = rng.choice([x for x in users if x[5] <= d.isoformat()] or users[:1])
            tier = u[4]
            cat = rng.choices(list(cat_weight), weights=list(cat_weight.values()))[0]
            p = rng.choice([x for x in products if x[2] == cat])
            qty = rng.choices([1, 2, 3], weights=[80, 15, 5])[0]
            if tier != "normal" and rng.random() < 0.35:
                qty += 1  # 会员买得更多，客单价更高
            cid, disc = None, 0.0
            for c_id, s, e, r in camp:
                if s <= d <= e and rng.random() < 0.85:
                    cid, disc = c_id, r
            if tier == "svip":
                disc += 0.05
            amount = round(p[4] * qty * (1 - disc), 2)
            mi = month_index(d)
            channel = rng.choices(
                ["app", "web", "mini_program"], weights=[50, max(10, 35 - mi), 15 + mi * 1.2]
            )[0]
            created = ts(d, rng)
            r = rng.random()
            if r < 0.06:
                status, paid, delivered = "cancelled", None, None
            else:
                paid = created + timedelta(minutes=rng.randint(1, 90))
                age = (END - d).days
                if age < 2:
                    status, delivered = rng.choice(["pending", "paid"]), None
                    if status == "pending":
                        paid = None
                elif age < 6 and rng.random() < 0.5:
                    status, delivered = "shipped", None
                else:
                    status = "delivered"
                    delivered = paid + timedelta(days=rng.randint(1, 6), hours=rng.randint(0, 20))
            orders.append(
                [
                    f"O{oid:06d}",
                    u[0],
                    p[0],
                    qty,
                    amount,
                    cid,
                    channel,
                    status,
                    created.strftime("%Y-%m-%d %H:%M:%S"),
                    paid.strftime("%Y-%m-%d %H:%M:%S") if paid else None,
                    delivered.strftime("%Y-%m-%d %H:%M:%S") if delivered else None,
                ]
            )

            # 售后：已签收订单约 11% 申请；数码 2026 年逐月上升；美妆低
            if status != "delivered":
                continue
            base = {"apparel": 0.14, "digital": 0.08, "home": 0.09, "food": 0.04, "beauty": 0.05}[
                cat
            ]
            if cat == "digital" and d.year == 2026:
                base += 0.02 * d.month
            if cid is not None:
                base += 0.03  # 大促冲动消费，售后更多
            if rng.random() >= base:
                continue
            tid += 1
            ttype = rng.choices(
                ["refund", "return", "exchange"],
                weights=[20, 55, 25] if cat == "apparel" else [30, 55, 15],
            )[0]
            reason = rng.choices(
                ["quality", "wrong_item", "size", "late", "no_reason"],
                weights=[30, 8, 35 if cat == "apparel" else 2, 12, 25],
            )[0]
            tcreated = delivered + timedelta(days=rng.randint(0, 6), hours=rng.randint(0, 23))
            tstatus = rng.choices(
                ["approved", "rejected", "closed", "open"], weights=[25, 10, 60, 5]
            )[0]
            if (END - tcreated.date()).days < 3:
                tstatus = "open"
            refund = 0.0
            if ttype != "exchange" and tstatus in ("approved", "closed"):
                refund = (
                    amount if rng.random() < 0.85 else round(amount * rng.choice([0.3, 0.5]), 2)
                )
            closed = (
                (tcreated + timedelta(days=rng.randint(1, 7)))
                if tstatus in ("closed", "rejected")
                else None
            )
            tickets.append(
                (
                    f"T{tid:05d}",
                    f"O{oid:06d}",
                    ttype,
                    reason,
                    tstatus,
                    refund,
                    tcreated.strftime("%Y-%m-%d %H:%M:%S"),
                    closed.strftime("%Y-%m-%d %H:%M:%S") if closed else None,
                )
            )
            # 全额退款完结的订单，订单状态改为 refunded
            if refund == amount and tstatus == "closed":
                orders[-1][7] = "refunded"

    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?)", tickets)
    conn.commit()
    conn.close()
    print(
        f"写入 {OUT.relative_to(ROOT)}：用户 {len(users)}、商品 {len(products)}、活动 {len(CAMPAIGNS)}、"
        f"订单 {len(orders)}、售后 {len(tickets)}"
    )


if __name__ == "__main__":
    sys.exit(main())
