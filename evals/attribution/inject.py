"""往 retail_ops 的副本里注入原因已知的变化，并按规则算出"哪些说法算答对"。

五种基本操作：删单据、删明细行、改价 / 改量、复制单据（可改成退货或无编号）、插入大额退货单。
每次操作都把被动到的明细行连同它当时的各维度取值记进一张临时表 touch（TEMP 表，只在本连接里，
不会留在库文件里，Agent 能查库，不能让它看到答案）。

标准答案（见 评测方案.md 第 3 节）：注入时指定的原因是主答案；另外按下面的规则补充"等价说法"：
某个维度取值同时满足
  影响占比 = 该取值内被动到的金额变化 ÷ 全部注入的金额变化 ≥ 0.8
  覆盖率   = 该取值内被动到的行的金额 ÷ 该取值在同一时间段、同一单据类型下的全部金额 ≥ 0.8
就算等价。前者保证它说的是这次注入，后者排除"英国"这类本来就占大头的粗粒度说法。
"""

from __future__ import annotations

import random
import sqlite3

DIMS = {
    "market": "c.market",
    "country": "i.country",
    "category": "l.category",
    "customer_type": "i.customer_type",
    "value_tier": "cu.value_tier",
    "size_band": "i.size_band",
    "customer": "i.customer_id",
    "product": "l.stock_code",
    "invoice": "i.invoice_no",
}
JOIN = (
    "invoice_lines l JOIN invoices i ON l.invoice_no = i.invoice_no "
    "JOIN countries c ON i.country = c.country "
    "LEFT JOIN customers cu ON i.customer_id = cu.customer_id"
)
EQUIV_EFFECT, EQUIV_COVER = 0.8, 0.8
LARGE, MEDIUM = 5000.0, 1000.0


def _band(col: str = "amount") -> str:
    return (
        f"CASE WHEN {col} >= {LARGE} THEN 'large' WHEN {col} >= {MEDIUM} THEN 'medium' "
        "ELSE 'small' END"
    )


class Injector:
    def __init__(self, conn: sqlite3.Connection, period: tuple[str, str], seed: int = 0):
        self.conn = conn
        self.period = period
        self.rng = random.Random(seed)
        cols = ", ".join(f"{k} TEXT" for k in DIMS)
        conn.execute("DROP TABLE IF EXISTS temp.touch")
        conn.execute(f"CREATE TEMP TABLE touch ({cols}, itype TEXT, volume REAL, effect REAL)")
        self.volume: dict[tuple[str, str, str], float] = {}  # (单据类型, 维度, 取值) → 全部金额

    # --- 记录 ---

    def _in_period(self, alias: str = "i") -> str:
        a, b = self.period
        return f"{alias}.invoice_date >= '{a}' AND {alias}.invoice_date < '{b}'"

    def _snapshot(self, itype: str) -> None:
        """记下此刻（被动到的行都还在 / 已经在）各维度取值在本时间段的全部金额，算覆盖率用。"""
        for dim, expr in DIMS.items():
            rows = self.conn.execute(
                f"SELECT CAST({expr} AS TEXT), SUM(l.amount) FROM {JOIN} "
                f"WHERE i.invoice_type = ? AND {self._in_period()} GROUP BY 1",
                (itype,),
            )
            for value, vol in rows:
                key = (itype, dim, value)
                self.volume[key] = max(self.volume.get(key, 0.0), vol or 0.0)

    def _touch(self, where: str, effect: str, itype: str) -> None:
        dims = ", ".join(f"CAST({e} AS TEXT)" for e in DIMS.values())
        self.conn.execute(
            f"INSERT INTO touch SELECT {dims}, i.invoice_type, l.amount, {effect} "
            f"FROM {JOIN} WHERE i.invoice_type = '{itype}' AND {self._in_period()} AND ({where})"
        )
        self._snapshot(itype)

    def _resync(self, invoice_filter: str) -> None:
        """明细行变了以后，让发票表的金额、分档和明细一致；没有明细行的发票删掉。"""
        c = self.conn
        c.execute(
            f"UPDATE invoices SET amount = COALESCE((SELECT ROUND(SUM(amount), 2) "
            f"FROM invoice_lines WHERE invoice_lines.invoice_no = invoices.invoice_no), 0) "
            f"WHERE {invoice_filter}"
        )
        c.execute(f"UPDATE invoices SET size_band = {_band()} WHERE {invoice_filter}")
        c.execute(
            f"DELETE FROM invoices WHERE ({invoice_filter}) AND invoice_no NOT IN "
            "(SELECT DISTINCT invoice_no FROM invoice_lines)"
        )

    def _new_numbers(self, n: int, prefix: str = "") -> list[str]:
        used = {r[0] for r in self.conn.execute("SELECT invoice_no FROM invoices")}
        out = []
        while len(out) < n:
            no = f"{prefix}{self.rng.randint(489000, 581999)}"
            if no not in used:
                used.add(no)
                out.append(no)
        return out

    # --- 五种操作（where 写在 l / i / c / cu 这几个别名上） ---

    def delete_invoices(self, where: str, itype: str = "sale", fraction: float = 1.0) -> None:
        ids = [
            r[0]
            for r in self.conn.execute(
                f"SELECT i.invoice_no FROM invoices i JOIN countries c ON i.country = c.country "
                f"LEFT JOIN customers cu ON i.customer_id = cu.customer_id "
                f"WHERE i.invoice_type = ? AND {self._in_period()} AND ({where}) "
                "ORDER BY i.invoice_no",
                (itype,),
            )
        ]
        if fraction < 1.0:
            ids = sorted(self.rng.sample(ids, round(len(ids) * fraction)))
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS pick (invoice_no TEXT PRIMARY KEY)")
        self.conn.execute("DELETE FROM pick")
        self.conn.executemany("INSERT INTO pick VALUES (?)", [(x,) for x in ids])
        self._touch("i.invoice_no IN (SELECT invoice_no FROM pick)", "-l.amount", itype)
        self.conn.execute(
            "DELETE FROM invoice_lines WHERE invoice_no IN (SELECT invoice_no FROM pick)"
        )
        self.conn.execute("DELETE FROM invoices WHERE invoice_no IN (SELECT invoice_no FROM pick)")

    def delete_lines(self, where: str, itype: str = "sale") -> None:
        self._touch(where, "-l.amount", itype)
        self.conn.execute(
            f"DELETE FROM invoice_lines WHERE line_id IN (SELECT l.line_id FROM {JOIN} "
            f"WHERE i.invoice_type = '{itype}' AND {self._in_period()} AND ({where}))"
        )
        a, b = self.period
        self._resync(f"invoice_date >= '{a}' AND invoice_date < '{b}'")

    def scale_lines(self, where: str, price: float = 1.0, qty: float = 1.0) -> None:
        self._touch(where, f"l.amount * ({price * qty} - 1)", "sale")
        self.conn.execute(
            f"UPDATE invoice_lines SET unit_price = ROUND(unit_price * {price}, 2), "
            f"quantity = MAX(1, CAST(ROUND(quantity * {qty}) AS INTEGER)), "
            f"amount = ROUND(amount * {price * qty}, 2) WHERE line_id IN "
            f"(SELECT l.line_id FROM {JOIN} WHERE i.invoice_type = 'sale' "
            f"AND {self._in_period()} AND ({where}))"
        )
        a, b = self.period
        self._resync(f"invoice_date >= '{a}' AND invoice_date < '{b}'")

    def clone_invoices(
        self,
        where: str,
        times: float = 1.0,
        as_type: str = "sale",
        guest: bool = False,
    ) -> list[str]:
        """把符合条件的销售单复制成新单据（times 可以小于 1：只复制一部分）。
        as_type='return' 复制成退货单；guest=True 去掉客户编号。时间在原时间上顺延几分钟。"""
        src = [
            r[0]
            for r in self.conn.execute(
                f"SELECT i.invoice_no FROM invoices i JOIN countries c ON i.country = c.country "
                f"LEFT JOIN customers cu ON i.customer_id = cu.customer_id "
                f"WHERE i.invoice_type = 'sale' AND {self._in_period()} AND ({where}) "
                "ORDER BY i.invoice_no"
            )
        ]
        whole, part = int(times), times - int(times)
        picked = src * whole + sorted(self.rng.sample(src, round(len(src) * part)))
        new = self._new_numbers(len(picked), "C" if as_type == "return" else "")
        for old, no in zip(picked, new, strict=True):
            self.conn.execute(
                "INSERT INTO invoices SELECT ?, ?, datetime(invoice_date, ?), "
                + ("NULL" if guest else "customer_id")
                + ", country, "
                + ("'guest'" if guest else "customer_type")
                + ", size_band, amount FROM invoices WHERE invoice_no = ?",
                (no, as_type, f"+{self.rng.randint(3, 240)} minutes", old),
            )
            self.conn.execute(
                "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, "
                "unit_price, amount) SELECT ?, stock_code, category, quantity, unit_price, "
                "amount FROM invoice_lines WHERE invoice_no = ?",
                (no, old),
            )
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS pick (invoice_no TEXT PRIMARY KEY)")
        self.conn.execute("DELETE FROM pick")
        self.conn.executemany("INSERT INTO pick VALUES (?)", [(x,) for x in new])
        # 顺延后跨到下个月的，拉回月末
        end = self.period[1]
        self.conn.execute(
            "UPDATE invoices SET invoice_date = datetime(?, '-1 hour') "
            "WHERE invoice_date >= ? AND invoice_no IN (SELECT invoice_no FROM pick)",
            (end + " 00:00:00", end),
        )
        self._touch("i.invoice_no IN (SELECT invoice_no FROM pick)", "l.amount", as_type)
        return new

    def insert_returns(
        self, customer_id: int, lines: list[tuple[str, int, float]], days: list[int]
    ) -> list[str]:
        """插入大额退货单：每张一行（商品、件数、单价），日期是本时间段的第几天。"""
        new = self._new_numbers(len(lines), "C")
        country = self.conn.execute(
            "SELECT country FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone()[0]
        for no, (stock, qty, price), day in zip(new, lines, days, strict=True):
            amount = round(qty * price, 2)
            cat = self.conn.execute(
                "SELECT category FROM invoice_lines WHERE stock_code = ? LIMIT 1", (stock,)
            ).fetchone()[0]
            self.conn.execute(
                f"INSERT INTO invoices VALUES (?, 'return', datetime(?, ?), ?, ?, 'returning', "
                f"{_band('?')}, ?)",
                (no, self.period[0] + " 10:00:00", f"+{day} days", customer_id, country,
                 amount, amount, amount),
            )  # fmt: skip
            self.conn.execute(
                "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, "
                "unit_price, amount) VALUES (?, ?, ?, ?, ?, ?)",
                (no, stock, cat, qty, price, amount),
            )
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS pick (invoice_no TEXT PRIMARY KEY)")
        self.conn.execute("DELETE FROM pick")
        self.conn.executemany("INSERT INTO pick VALUES (?)", [(x,) for x in new])
        self._touch("i.invoice_no IN (SELECT invoice_no FROM pick)", "l.amount", "return")
        return new

    # --- 标准答案 ---

    def equivalents(self) -> list[list[list[str]]]:
        """按"影响占比 ≥ 0.8 且覆盖率 ≥ 0.8"找出的等价说法，每个是 [[维度, 取值]]。"""
        total = self.conn.execute("SELECT SUM(ABS(effect)) FROM touch").fetchone()[0] or 0.0
        out = []
        if not total:
            return out
        for dim in DIMS:
            rows = self.conn.execute(
                f"SELECT {dim}, itype, SUM(ABS(effect)), SUM(volume) FROM touch "
                f"WHERE {dim} IS NOT NULL GROUP BY 1, 2"
            ).fetchall()
            by: dict[str, list[float]] = {}
            for value, itype, eff, vol in rows:
                acc = by.setdefault(value, [0.0, 0.0, 0.0])
                acc[0] += eff
                acc[1] += vol
                acc[2] += self.volume.get((itype, dim, value), 0.0)
            for value, (eff, vol, whole) in by.items():
                if eff / total >= EQUIV_EFFECT and whole and vol / whole >= EQUIV_COVER:
                    out.append([[dim, value]])
        return out

    def finish(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS temp.touch")
        self.conn.execute("DROP TABLE IF EXISTS temp.pick")
        self.conn.commit()


def metric_value(conn: sqlite3.Connection, metric: str, period: tuple[str, str]) -> float:
    """直接在发票表上算指标（不经过指标字典和归因引擎，用来独立地量注入的效果）。"""
    a, b = period
    gmv, ret, orders = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN invoice_type = 'sale' THEN amount END), 0), "
        "COALESCE(SUM(CASE WHEN invoice_type = 'return' THEN amount END), 0), "
        "SUM(invoice_type = 'sale') FROM invoices WHERE invoice_date >= ? AND invoice_date < ?",
        (a, b),
    ).fetchone()
    return {
        "gmv": gmv,
        "return_amount": ret,
        "net_sales": gmv - ret,
        "return_rate": ret / gmv if gmv else 0.0,
        "order_count": float(orders or 0),
        "aov": gmv / orders if orders else 0.0,
    }[metric]


# 注入类型 → 怎么动数据。params 里的取值都来自 cases.json，这里不做任何随机选择以外的决定。
def apply(conn: sqlite3.Connection, case: dict) -> Injector:
    inj = Injector(conn, tuple(case["current"]), case.get("seed", 0))
    for op in case["ops"]:
        kind, p = op["op"], op
        if kind == "delete_invoices":
            inj.delete_invoices(p["where"], p.get("itype", "sale"), p.get("fraction", 1.0))
        elif kind == "delete_lines":
            inj.delete_lines(p["where"])
        elif kind == "scale_lines":
            inj.scale_lines(p["where"], p.get("price", 1.0), p.get("qty", 1.0))
        elif kind == "clone_invoices":
            inj.clone_invoices(
                p["where"], p.get("times", 1.0), p.get("as_type", "sale"), p.get("guest", False)
            )
        elif kind == "insert_returns":
            inj.insert_returns(p["customer_id"], [tuple(x) for x in p["lines"]], p["days"])
        else:
            raise ValueError(f"未知操作：{kind}")
    return inj
