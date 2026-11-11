"""生成归因评测集 evals/attribution/cases.json（构造方法见同目录 评测方案.md，先定后跑）。

两部分：
- 真实案例：指标变化来自原始数据本身，标准答案取自看板项目的分析产物
  （site/findings.json 的结论，和用它的 site/data.json 按同样方法重算的月度退货单排名），
  不经过本仓库的归因代码。
- 注入异常：在库的副本里按固定种子制造一个原因已知的变化；只保留"注入的效果占观测到的
  总变化 ≥ 60%"的方案，保证注入的原因确实是主因。

用法：
  uv run python evals/attribution/build_cases.py --dashboard ../ecommerce-dashboard
不调用模型、不花钱。同样的库和种子重跑得到同一份 cases.json。
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from datapilot import config  # noqa: E402
from evals.attribution.inject import DIMS, JOIN, Injector, apply, metric_value  # noqa: E402

OUT = Path(__file__).with_name("cases.json")
WORK = config.DATA_DIR / "attribution_dbs" / "_work.sqlite"
SEED = 20261111
MIN_EFFECT_SHARE = 0.6  # 注入效果 ÷ 观测到的总变化：下限保证它是主因，
MAX_EFFECT_SHARE = 2.0  # 上限排除"注入的变化被反方向的自然波动抵消了大半"的题
MIN_RELATIVE_CHANGE = 0.01  # 总变化至少是基期的 1%
MONTHS = [f"{y}-{m:02d}" for y in (2010, 2011) for m in range(1, 13)][:-1]  # 2010-01 … 2011-11

# (类型, 指标) ， dev 调阈值和排序方法用；holdout 在方案定稿后只跑一次，并含 dev 里没有的三种类型
DEV_SLOTS = [
    ("country_drop", "gmv"), ("country_drop", "gmv"), ("country_drop", "order_count"),
    ("country_surge", "gmv"), ("country_surge", "gmv"),
    ("category_price_cut", "gmv"), ("category_price_cut", "gmv"), ("category_price_cut", "aov"),
    ("category_promo", "gmv"), ("category_promo", "gmv"),
    ("large_returns", "return_rate"), ("large_returns", "return_rate"),
    ("large_returns", "net_sales"),
    ("key_account_churn", "gmv"), ("key_account_churn", "gmv"),
    ("new_customer_drop", "gmv"), ("new_customer_drop", "order_count"),
    ("guest_surge", "gmv"),
    ("product_stockout", "gmv"), ("product_stockout", "gmv"),
    ("cross_drop", "gmv"),
    ("two_causes", "gmv"),
]  # fmt: skip
HOLDOUT_SLOTS = [
    ("country_drop", "gmv"), ("country_drop", "order_count"),
    ("country_surge", "gmv"),
    ("category_price_cut", "gmv"), ("category_price_cut", "aov"),
    ("category_promo", "gmv"),
    ("large_returns", "return_rate"), ("large_returns", "net_sales"),
    ("key_account_churn", "gmv"), ("key_account_churn", "gmv"),
    ("new_customer_drop", "gmv"),
    ("guest_surge", "gmv"),
    ("product_stockout", "gmv"),
    ("cross_drop", "gmv"), ("cross_drop", "gmv"),
    ("two_causes", "gmv"), ("two_causes", "gmv"),
    ("tier_drop", "gmv"), ("tier_drop", "gmv"),  # 以下三种只在留出集出现
    ("large_sale_orders", "gmv"), ("large_sale_orders", "aov"),
    ("country_return_wave", "return_rate"),
]  # fmt: skip

METRIC_WORDS = {
    "gmv": ["GMV", "销售额", "成交额"],
    "return_rate": ["退货率"],
    "net_sales": ["净销售额"],
    "aov": ["客单价"],
    "order_count": ["订单数", "订单量"],
}
UP, DOWN = ["涨", "上升", "升高"], ["降", "下滑", "掉"]
TEMPLATES = [
    "{cur}{metric}为什么比{base}{dir}了这么多？",
    "{cur}的{metric}怎么回事，{cmp}{dir}得厉害",
    "{metric}在{cur}{dir}的原因是什么？对比{base}",
    "帮我分析一下{cur}{metric}{dir}是什么导致的（对比{base}）",
    "{cur}{metric}{dir}了，为什么？和{base}比",
]


def spaced(text: str) -> str:
    """中文和数字 / 英文之间补一个空格（题目是拼出来的，统一成正常的写法）。"""
    out = []
    for ch in text:
        if out and ch != " " and out[-1] != " ":
            a, b = out[-1], ch
            if (a.isascii() and a.isalnum() and "\u4e00" <= b <= "\u9fff") or (
                b.isascii() and b.isalnum() and "\u4e00" <= a <= "\u9fff"
            ):
                out.append(" ")
        out.append(ch)
    return "".join(out)


def month_period(m: str) -> tuple[str, str]:
    y, mo = int(m[:4]), int(m[5:])
    end = date(y + (mo == 12), mo % 12 + 1, 1)
    return f"{m}-01", end.isoformat()


def month_text(m: str) -> str:
    return f"{int(m[:4])} 年 {int(m[5:])} 月"


def prev_month(m: str, n: int = 1) -> str:
    d = date(int(m[:4]), int(m[5:]), 1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
    return d.strftime("%Y-%m")


def q1(conn, sql: str, args=()) -> list:
    return conn.execute(sql, args).fetchall()


def month_gmv(conn, m: str) -> float:
    return metric_value(conn, "gmv", month_period(m))


def share_rows(conn, m: str, expr: str, join_lines: bool = False) -> list[tuple]:
    """本月各取值的销售额占比，从大到小。"""
    a, b = month_period(m)
    src = (
        "invoice_lines l JOIN invoices i ON l.invoice_no = i.invoice_no"
        if join_lines
        else "invoices i"
    )
    amt = "l.amount" if join_lines else "i.amount"
    total = month_gmv(conn, m)
    rows = q1(
        conn,
        f"SELECT {expr}, SUM({amt}) FROM {src} LEFT JOIN customers cu "
        "ON i.customer_id = cu.customer_id WHERE i.invoice_type = 'sale' "
        "AND i.invoice_date >= ? AND i.invoice_date < ? GROUP BY 1 ORDER BY 2 DESC",
        (a, b),
    )
    return [(v, amt / total) for v, amt in rows if v is not None]


def pick(rng, rows, lo: float, hi: float = 1.0):
    ok = [v for v, s in rows if lo <= s <= hi]
    return rng.choice(ok) if ok else None


def lit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


# ---------- 各类型怎么选目标、怎么动数据 ----------


def plan_slot(conn, rng, kind: str, m: str) -> dict | None:
    """返回 {ops, primary[, secondary]}；这个月找不到合适的目标时返回 None。"""
    countries = [r for r in share_rows(conn, m, "i.country") if r[0] != "United Kingdom"]
    cats = [r for r in share_rows(conn, m, "l.category", True) if r[0] != "其他"]
    if kind == "country_drop":
        c = pick(rng, countries, 0.015)
        return c and {
            "ops": [{"op": "delete_invoices", "where": f"i.country = {lit(c)}"}],
            "primary": [["country", c]],
        }
    if kind == "country_surge":
        c = pick(rng, countries, 0.01)
        return c and {
            "ops": [
                {
                    "op": "clone_invoices",
                    "where": f"i.country = {lit(c)}",
                    "times": rng.choice([1.0, 1.5, 2.0]),
                }
            ],
            "primary": [["country", c]],
        }
    if kind in ("category_price_cut", "category_promo"):
        c = pick(rng, cats, 0.03, 0.3)
        change = (
            {"price": rng.choice([0.5, 0.6, 0.7])}
            if kind == "category_price_cut"
            else {"qty": rng.choice([1.5, 2.0, 3.0])}
        )
        return c and {
            "ops": [{"op": "scale_lines", "where": f"l.category = {lit(c)}", **change}],
            "primary": [["category", c]],
        }
    if kind == "large_returns":
        a, b = month_period(m)
        buyers = q1(
            conn,
            "SELECT i.customer_id FROM invoices i JOIN customers cu USING (customer_id) "
            "WHERE cu.value_tier = 'A' AND cu.country = 'United Kingdom' "
            "AND i.invoice_type = 'sale' AND i.invoice_date >= ? AND i.invoice_date < ? "
            "GROUP BY 1 ORDER BY 1",
            (a, b),
        )
        goods = q1(
            conn,
            "SELECT l.stock_code, AVG(l.unit_price) FROM invoice_lines l JOIN invoices i "
            "USING (invoice_no) WHERE i.invoice_type = 'sale' AND i.invoice_date >= ? "
            "AND i.invoice_date < ? GROUP BY 1 ORDER BY SUM(l.amount) DESC LIMIT 30",
            (a, b),
        )
        n = rng.choice([1, 1, 2, 3])
        target = month_gmv(conn, m) * rng.uniform(0.025, 0.07)
        lines = []
        for _ in range(n):
            stock, price = rng.choice(goods)
            price = round(price, 2)
            lines.append([stock, max(1, round(target / n / price)), price])
        return {
            "ops": [
                {
                    "op": "insert_returns",
                    "customer_id": rng.choice(buyers)[0],
                    "lines": lines,
                    "days": sorted(rng.sample(range(2, 26), n)),
                }
            ],
            "primary": None,  # 注入后才知道发票号，由 simulate 填
        }
    if kind == "key_account_churn":
        c = pick(rng, share_rows(conn, m, "i.customer_id"), 0.025)
        return c and {
            "ops": [{"op": "delete_invoices", "where": f"i.customer_id = {int(c)}"}],
            "primary": [["customer", str(c)]],
        }
    if kind == "new_customer_drop":
        return {
            "ops": [
                {
                    "op": "delete_invoices",
                    "where": "i.customer_type = 'new'",
                    "fraction": rng.choice([0.8, 0.9, 1.0]),
                }
            ],
            "primary": [["customer_type", "new"]],
        }
    if kind == "guest_surge":
        return {
            "ops": [
                {
                    "op": "clone_invoices",
                    "where": "i.customer_type = 'guest'",
                    "times": rng.choice([1.0, 1.5, 2.0]),
                }
            ],
            "primary": [["customer_type", "guest"]],
        }
    if kind == "product_stockout":
        s = pick(rng, share_rows(conn, m, "l.stock_code", True)[:40], 0.012)
        return s and {
            "ops": [{"op": "delete_lines", "where": f"l.stock_code = {lit(s)}"}],
            "primary": [["product", s]],
        }
    if kind == "cross_drop":
        c = pick(rng, cats, 0.05, 0.3)
        country = rng.choice(["United Kingdom", "United Kingdom", *[r[0] for r in countries[:3]]])
        return c and {
            "ops": [
                {
                    "op": "delete_lines",
                    "where": f"i.country = {lit(country)} AND l.category = {lit(c)}",
                }
            ],
            "primary": [["country", country], ["category", c]],
        }
    if kind == "two_causes":
        country, cat = pick(rng, countries, 0.02), pick(rng, cats, 0.05, 0.3)
        return (
            country
            and cat
            and {
                "ops": [
                    {"op": "delete_invoices", "where": f"i.country = {lit(country)}"},
                    {
                        "op": "scale_lines",
                        "where": f"l.category = {lit(cat)}",
                        "price": rng.choice([0.6, 0.7, 0.8]),
                    },
                ],
                "primary": [["country", country]],
                "secondary": [["category", cat]],
            }
        )
    if kind == "tier_drop":
        t = rng.choice(["B", "B", "C"])
        return {
            "ops": [
                {
                    "op": "delete_invoices",
                    "where": f"cu.value_tier = {lit(t)}",
                    "fraction": rng.choice([0.8, 0.9, 1.0]),
                }
            ],
            "primary": [["value_tier", t]],
        }
    if kind == "large_sale_orders":
        a, b = month_period(m)
        top = q1(
            conn,
            "SELECT invoice_no FROM invoices WHERE invoice_type = 'sale' AND invoice_date >= ? "
            "AND invoice_date < ? ORDER BY amount DESC LIMIT ?",
            (a, b, rng.choice([1, 2])),
        )
        ids = ", ".join(lit(r[0]) for r in top)
        return {
            "ops": [
                {
                    "op": "clone_invoices",
                    "where": f"i.invoice_no IN ({ids})",
                    "times": float(rng.choice([1, 2])),
                }
            ],
            "primary": None,
        }
    if kind == "country_return_wave":
        c = pick(rng, countries, 0.02)
        return c and {
            "ops": [
                {
                    "op": "clone_invoices",
                    "where": f"i.country = {lit(c)}",
                    "times": rng.choice([0.5, 0.75, 1.0]),
                    "as_type": "return",
                }
            ],
            "primary": [["country", c]],
        }
    raise ValueError(kind)


def dominates(conn, case: dict, conds: list, itype: str) -> bool:
    """入选规则 3：在它自己的维度里，注入的那一格是"偏离同比例变化最多"的一格。
    偏离 = 该格的变化 − 该格基期份额 × 整体变化。同一维度里如果有别的格自然波动得更厉害，
    说"注入的才是原因"就站不住，这样的方案不要。量的是该单据类型的金额（订单数的题量单数）。"""
    key = " || '|' || ".join(f"COALESCE(CAST({DIMS[d]} AS TEXT), '')" for d, _ in conds)
    gold = "|".join(str(v) for _, v in conds)
    (bs, be), (cs, ce) = case["base"], case["current"]
    measure = (
        "COUNT(DISTINCT CASE WHEN {p} THEN i.invoice_no END)"
        if case["metric"] == "order_count"
        else "COALESCE(SUM(CASE WHEN {p} THEN l.amount END), 0)"
    )
    rows = conn.execute(
        f"SELECT {key}, "
        + measure.format(p=f"i.invoice_date >= '{bs}' AND i.invoice_date < '{be}'")
        + ", "
        + measure.format(p=f"i.invoice_date >= '{cs}' AND i.invoice_date < '{ce}'")
        + f" FROM {JOIN} WHERE i.invoice_type = ? AND ((i.invoice_date >= '{bs}' "
        f"AND i.invoice_date < '{be}') OR (i.invoice_date >= '{cs}' AND i.invoice_date < '{ce}')) "
        "GROUP BY 1",
        (itype,),
    ).fetchall()
    t0, t1 = sum(r[1] for r in rows), sum(r[2] for r in rows)
    if not t0 or t1 == t0:
        return False
    excess = {k: (c - b) - b / t0 * (t1 - t0) for k, b, c in rows}
    mine = excess.get(gold)
    if mine is None or mine * (t1 - t0) <= 0:
        return False
    return all(abs(mine) >= abs(x) for k, x in excess.items() if k != gold and x * mine > 0)


RETURN_TYPES = {"large_returns", "country_return_wave"}


def simulate(conn, case: dict) -> dict | None:
    """在事务里注入、量效果、算等价说法，然后回滚。不满足入选规则返回 None。"""
    cur, base = tuple(case["current"]), tuple(case["base"])
    metric = case["metric"]
    before = metric_value(conn, metric, cur)
    m0 = metric_value(conn, metric, base)
    try:
        steps = []
        inj: Injector | None = None
        for i in range(len(case["ops"])):
            # 逐个操作量效果（两个原因的题要分别知道各自的大小）；等价说法只在单一原因时算
            one = {**case, "ops": case["ops"][i : i + 1], "seed": case["seed"] + i}
            prev = metric_value(conn, metric, cur)
            inj = apply(conn, one)
            steps.append(metric_value(conn, metric, cur) - prev)
            new_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT invoice FROM temp.touch GROUP BY 1 ORDER BY SUM(effect) DESC, 1"
                )
            ]
            equivalents = inj.equivalents() if len(case["ops"]) == 1 else []
        after = metric_value(conn, metric, cur)
        itype = "return" if case["type"] in RETURN_TYPES else "sale"
        checks = [c for c in (case["primary"], case.get("secondary")) if c]
        if case["primary"] is None:
            checks = [[["invoice", new_ids[0]]]]
        dominant = all(dominates(conn, case, c, itype) for c in checks)
        sources = []
        if case["type"] == "large_sale_orders":
            # 被复制的原单和复制件金额、客户、商品都一样，从数据上分不出来，都算对
            marks = ", ".join("?" * len(new_ids))
            sources = [
                r[0]
                for r in conn.execute(
                    "SELECT a.invoice_no FROM invoices a WHERE a.invoice_type = 'sale' "
                    "AND a.invoice_date >= ? AND a.invoice_date < ? AND EXISTS (SELECT 1 "
                    "FROM invoices b WHERE b.invoice_no <> a.invoice_no AND b.amount = a.amount "
                    f"AND b.customer_id IS a.customer_id AND b.invoice_no IN ({marks})) ORDER BY 1",
                    (*cur, *new_ids),
                )
            ]
    finally:
        conn.rollback()
    if not dominant:
        return None
    effect, delta = after - before, after - m0
    if delta == 0 or m0 == 0:
        return None
    if not MIN_EFFECT_SHARE <= effect / delta <= MAX_EFFECT_SHARE:
        return None
    if abs(delta / m0) < MIN_RELATIVE_CHANGE:
        return None
    primary = case["primary"]
    gold = []
    if primary is None:  # 插入的单据：每一张都算答对；金额最大的是主答案
        gold = [[["invoice", no]] for no in dict.fromkeys([*new_ids, *sources])]
        primary = gold[0]
    else:
        gold = [primary]
        if primary[0] == ["country", "United Kingdom"] and len(primary) == 2:
            gold.append([["market", "UK"], primary[1]])  # 英国这个国家和英国本土市场是同一批单据
    second = case.get("secondary")
    if second:
        if abs(steps[1]) > abs(steps[0]):
            primary, second = second, primary
        if min(abs(s) for s in steps) / abs(delta) < 0.2:
            return None  # 第二个原因太小，不算"两个原因"的题
        gold = [primary, second]
    for e in equivalents:
        if e not in gold:
            gold.append(e)
    return {
        **case,
        "primary": primary,
        "secondary": second,
        "gold": gold,
        "effect": round(effect, 6),
        "delta": round(delta, 6),
        "effect_share": round(effect / delta, 4),
        "base_value": round(m0, 6),
        "current_value": round(after, 6),
    }


def question_for(rng, metric: str, cur: str, base: str, up: bool) -> str:
    yoy = base == prev_month(cur, 12)
    word = rng.choice(METRIC_WORDS[metric])
    direction = rng.choice(UP if up else DOWN)
    if cur == "2011-11" and not yoy and rng.random() < 0.5:
        # 数据截止在 2011-11，"上个月"就是它；考相对时间的解析
        return spaced(f"上个月{word}为什么{direction}了？")
    text = rng.choice(TEMPLATES).format(
        cur=month_text(cur),
        metric=word,
        base=f"去年同期（{month_text(base)}）" if yoy else month_text(base),
        cmp="同比" if yoy else "环比",
        dir=direction,
    )
    return spaced(text)


def injected_cases(conn, split: str, slots: list, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for n, (kind, metric) in enumerate(slots, 1):
        for attempt in range(1500):
            cur = rng.choice(MONTHS)
            yoy = rng.random() < 0.25 and cur >= "2010-12"
            base = prev_month(cur, 12 if yoy else 1)
            plan = plan_slot(conn, rng, kind, cur)
            if not plan:
                continue
            case = {
                "id": f"{split}-{n:02d}",
                "split": split,
                "kind": "injected",
                "type": kind,
                "metric": metric,
                "current": list(month_period(cur)),
                "base": list(month_period(base)),
                "seed": seed * 1000 + n * 10 + attempt % 7,
                **plan,
            }
            done = simulate(conn, case)
            if done:
                done["question"] = question_for(rng, metric, cur, base, done["delta"] > 0)
                done["attempts"] = attempt + 1
                out.append(done)
                break
        else:
            raise RuntimeError(f"{split} 第 {n} 个（{kind} / {metric}）1500 次都没有满足入选规则")
    return out


# ---------- 真实案例 ----------


def real_cases(conn, dashboard: Path) -> list[dict]:
    data = json.loads((dashboard / "site" / "data.json").read_text(encoding="utf-8"))
    findings = json.loads((dashboard / "site" / "findings.json").read_text(encoding="utf-8"))
    start = date.fromisoformat(data["meta"]["start"])

    def table(t):
        return [
            ((start + timedelta(days=d)).isoformat(), v / 100)
            for d, v in zip(t["d"], t["v"], strict=True)
        ]

    orders, returns = table(data["orders"]), table(data["returns"])

    def invoice_no(day: str, amount: float) -> str:
        rows = q1(
            conn,
            "SELECT invoice_no FROM invoices WHERE invoice_type = 'return' "
            "AND substr(invoice_date, 1, 10) = ? AND ABS(amount - ?) < 0.006",
            (day, amount),
        )
        assert len(rows) == 1, (day, amount, rows)
        return rows[0][0]

    def equivalents(nos: list[str], period) -> list:
        inj = Injector(conn, period)
        ids = ", ".join(lit(n) for n in nos)
        inj._touch(f"i.invoice_no IN ({ids})", "l.amount", "return")
        out = inj.equivalents()
        conn.rollback()
        return out

    cases = []
    # 1. 月度退货率尖峰：环比升 ≥ 1.5 个百分点，且当月最大的 ≤ 3 张退货单占当月退货额 ≥ 50%
    months = sorted({d[:7] for d, _ in orders})
    gmv = {m: sum(v for d, v in orders if d[:7] == m) for m in months}
    ret = {m: sum(v for d, v in returns if d[:7] == m) for m in months}
    for prev, m in zip(months, months[1:], strict=False):
        rise = ret[m] / gmv[m] - ret[prev] / gmv[prev]
        top = sorted(((v, d) for d, v in returns if d[:7] == m), reverse=True)
        k, acc = 0, 0.0
        while acc < 0.5 * ret[m]:
            acc += top[k][0]
            k += 1
        if rise < 0.015 or k > 3:
            continue
        nos = [invoice_no(d, v) for v, d in top[:k]]
        period = month_period(m)
        gold = [[["invoice", n]] for n in nos]
        gold += [e for e in equivalents(nos, period) if e not in gold]
        cases.append(
            {
                "id": f"real-{len(cases) + 1:02d}",
                "split": "dev",
                "kind": "real",
                "type": "return_spike",
                "metric": "return_rate",
                "current": list(period),
                "base": list(month_period(prev)),
                "question": f"{int(m[:4])} 年 {int(m[5:])} 月退货率为什么比上个月高这么多？",
                "primary": gold[0],
                "gold": gold,
                "source": f"看板 site/data.json：当月退货率环比 +{rise * 100:.2f} 个百分点，"
                f"最大的 {k} 张退货单占当月退货额 {acc / ret[m]:.1%}（按看板发现 3 的方法）",
                "base_value": round(ret[prev] / gmv[prev], 6),
                "current_value": round(ret[m] / gmv[m], 6),
            }
        )
    # 2. 近 12 个月 vs 前 12 个月的退货率（看板发现 3：前 10 张退货单占退货额 44.6%，剔除后 1.74%）
    p1, p2 = ["2009-12-01", "2010-12-01"], ["2010-12-01", "2011-12-01"]
    top10 = sorted(((v, d) for d, v in returns if d >= p2[0]), reverse=True)[:10]
    share = sum(v for v, _ in top10) / sum(v for d, v in returns if d >= p2[0])
    assert round(share, 4) == findings["returns"]["top10_share_p2"], share
    nos = [invoice_no(d, v) for v, d in top10]
    gold = [[["invoice", n]] for n in nos] + [[["size_band", "large"]]]
    gold += [e for e in equivalents(nos[:1], tuple(p2)) if e not in gold]
    cases.append(
        {
            "id": f"real-{len(cases) + 1:02d}",
            "split": "dev",
            "kind": "real",
            "type": "return_concentration",
            "metric": "return_rate",
            "current": p2,
            "base": p1,
            "question": "近 12 个月的退货率为什么比前 12 个月高？",
            "primary": gold[0],
            "gold": gold,
            "source": "看板发现 3（findings.json returns.top10_share_p2 = 0.4462，"
            "return_rate_ex_top10_p2 = 0.0174）：答出前 10 张里的任意一张、"
            "或“大额退货单”这一档，算命中",
            "base_value": findings["kpi_p1"]["return_rate"],
            "current_value": findings["kpi_p2"]["return_rate"],
        }
    )
    # 3. 近 12 个月 GMV 增长：看板发现 1（海外 +12.1%、英国 +0.9%）和
    #    发现 5（无编号订单占比 11.3% → 14.6%）
    cases.append(
        {
            "id": f"real-{len(cases) + 1:02d}",
            "split": "dev",
            "kind": "real",
            "type": "growth_source",
            "metric": "gmv",
            "current": p2,
            "base": p1,
            "question": "近 12 个月 GMV 比前 12 个月涨了，主要是什么带来的？",
            "primary": [["market", "Overseas"]],
            "gold": [[["market", "Overseas"]], [["customer_type", "guest"]]],
            "source": f"看板发现 1（country.ov_yoy = {findings['country']['ov_yoy']}，"
            f"uk_yoy = {findings['country']['uk_yoy']}）和发现 5"
            f"（no_id_gmv_share {findings['kpi_p1']['no_id_gmv_share']} → "
            f"{findings['kpi_p2']['no_id_gmv_share']}）：答海外市场或无编号订单都算命中",
            "base_value": findings["kpi_p1"]["gmv"],
            "current_value": findings["kpi_p2"]["gmv"],
        }
    )
    return cases


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dashboard", type=Path, required=True, help="看板项目目录（读 site/*.json）")
    args = ap.parse_args()
    WORK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.db_path("retail_ops"), WORK)  # 在副本上模拟，原库不动
    conn = sqlite3.connect(WORK)
    try:
        cases = real_cases(conn, args.dashboard)
        cases += injected_cases(conn, "dev", DEV_SLOTS, SEED)
        cases += injected_cases(conn, "holdout", HOLDOUT_SLOTS, SEED + 1)
    finally:
        conn.close()
        WORK.unlink(missing_ok=True)
    OUT.write_text(json.dumps(cases, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    by = {}
    for c in cases:
        by[(c["split"], c["kind"])] = by.get((c["split"], c["kind"]), 0) + 1
    print(f"已写入 {OUT.relative_to(ROOT)}：{len(cases)} 例", by)
    for c in cases:
        share = f"{c['effect_share']:.0%}" if "effect_share" in c else "真实"
        print(f"  {c['id']:11s} {c['type']:20s} {c['metric']:12s} {share:>5s}  {c['question']}")


if __name__ == "__main__":
    main()
