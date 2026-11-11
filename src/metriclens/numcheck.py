"""数字核对：回答里的每个数字都要能在"已执行的 SQL 结果"或"由代码从 SQL 结果算出的事实"里找到。

做法：
1. 先把不是数据的数字遮掉：日期（2011-01、2011 年 1 月）、编号（发票号、客户编号、商品编码，
   取自 SQL 结果里的标识列）、序数（第 2、前 3、Top 5）、"近 12 个月"这类时间长度；
2. 剩下的每个数字连同单位（%、个百分点、万、亿）取出来，按它在回答里写的小数位数，
   和候选值逐个比较：候选值按同样位数四舍五入后相等就算对上。% 和百分点允许候选值是
   小数（0.0313 ↔ 3.13%），万 / 亿 允许候选值是原值；正负号不计（"下降 12%" 对应 −12）；
3. 对不上的数字原样返回，由调用方决定重写、改用模板或标注。

lenient 模式（只在对照评测里用，给没有事实表的流程一个"算术是否算对"的口径）：
额外允许同一张结果表里两个单元格的一步运算（差、比、变化率）。两两组合会带来偶然相等，
所以只作为参考口径，主口径是不带 lenient 的严格核对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import combinations

_DATE = re.compile(
    # 2011-01-18、2011 年 1 月（18 日）
    r"\d{4}\s*[-/年.]\s*\d{1,2}\s*(?:[-/月.]\s*\d{1,2}\s*日?|月)?"
    r"|\d{4}\s*年"
    r"|\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?(?:份)?"
    r"|\d{1,2}\s*[–—~至到-]\s*\d{1,2}\s*月"
    r"|\d{1,2}\s*日"
    r"|\d{1,2}:\d{2}(?::\d{2})?"
)
_ORDINAL = re.compile(
    r"第\s*\d+|前\s*\d+\s*(?:大|个|张|名|位|家|类|项)?|(?:top|Top|TOP)\s*-?\s*\d+"
    r"|\d+\s*个?(?:月|年|周|季度|天)(?!\d)"  # 近 12 个月、前 12 个月、7 天
    r"|\d+\s*(?:个|条|点|类|项|层|步)\s*(?:原因|维度|方面|结论|建议|发现|下钻)"
    r"|[（(]\s*\d+\s*[)）]|(?<![\d.])\d+\s*[、.．](?=\s*\D)"  # （1） 1、 1. 这类编号
)
_NUMBER = re.compile(
    # 前面不能紧挨英文字母、数字、小数点（那是编号或一个数的后半截）；紧挨汉字是正常的
    r"(?<![A-Za-z0-9_.])([-+−–]?)\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(%|％|个百分点|百分点|pp|万|亿|倍|k|K)?"
)
_UNIT_SCALES = {
    "%": (Decimal(1), Decimal(100)),
    "％": (Decimal(1), Decimal(100)),
    "个百分点": (Decimal(1), Decimal(100)),
    "百分点": (Decimal(1), Decimal(100)),
    "pp": (Decimal(1), Decimal(100)),
    "万": (Decimal(1), Decimal("0.0001")),
    "亿": (Decimal(1), Decimal("0.00000001")),
    "k": (Decimal(1), Decimal("0.001")),
    "K": (Decimal(1), Decimal("0.001")),
}


@dataclass
class NumberCheck:
    total: int = 0
    from_sql: int = 0  # 直接出自 SQL 结果单元格
    derived: int = 0  # 出自代码从 SQL 结果算出的事实（带公式和来源）
    lenient: int = 0  # 只在 lenient 模式下对上（同表两格的一步运算）
    unmatched: list[str] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return self.from_sql + self.derived + self.lenient

    @property
    def ok(self) -> bool:
        return not self.unmatched

    def to_json(self) -> dict:
        return {
            "total": self.total,
            "matched": self.matched,
            "from_sql": self.from_sql,
            "derived": self.derived,
            "lenient": self.lenient,
            "unmatched": self.unmatched,
        }


def _dec(v) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        try:
            d = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def _round_eq(candidate: Decimal, shown: Decimal, places: int) -> bool:
    q = Decimal(1).scaleb(-places)
    c = abs(candidate)
    return any(c.quantize(q, rounding=r) == shown for r in (ROUND_HALF_UP, ROUND_HALF_EVEN))


def mask_non_data(text: str, ids: set[str] | None = None) -> str:
    """把日期、编号、序数换成空格（长度不变不重要，只要不再被当成数字）。"""
    for token in sorted(ids or (), key=len, reverse=True):
        if len(token) >= 4:
            text = re.sub(rf"(?<![A-Za-z0-9_.]){re.escape(token)}(?![A-Za-z0-9_]|\.\d)", " ", text)
    text = _DATE.sub(" ", text)
    return _ORDINAL.sub(" ", text)


def extract_numbers(text: str, ids: set[str] | None = None) -> list[tuple[str, Decimal, int, str]]:
    """返回 (原文, 数值, 小数位数, 单位)。"""
    out = []
    for m in _NUMBER.finditer(mask_non_data(text, ids)):
        raw = m.group(2).replace(",", "")
        places = len(raw.split(".")[1]) if "." in raw else 0
        out.append((m.group(0).strip(), Decimal(raw), places, m.group(3) or ""))
    return out


def _matches(shown: Decimal, places: int, unit: str, candidates: list[Decimal]) -> bool:
    scales = _UNIT_SCALES.get(unit, (Decimal(1),))
    return any(_round_eq(c * s, shown, places) for c in candidates for s in scales)


def _one_step(table: list[Decimal]) -> list[Decimal]:
    """同一张结果表里两个单元格的一步运算：差、比、变化率。"""
    out = []
    for a, b in combinations(table, 2):
        out.append(a - b)
        for x, y in ((a, b), (b, a)):
            if y != 0:
                out.append(x / y)
                out.append((x - y) / y)
    return out


def check_numbers(
    text: str,
    sql_tables: list[list[list]],
    derived: list[float] | None = None,
    ids: set[str] | None = None,
    lenient: bool = False,
) -> NumberCheck:
    """sql_tables：每条已执行 SQL 的结果行；derived：代码算出的事实值；ids：标识列的取值。"""
    cells_by_table = [
        [d for row in rows for v in row if (d := _dec(v)) is not None] for rows in sql_tables
    ]
    cells = [d for t in cells_by_table for d in t]
    facts = [d for v in (derived or []) if (d := _dec(v)) is not None]
    extra: list[Decimal] | None = None
    res = NumberCheck()
    for raw, shown, places, unit in extract_numbers(text, ids):
        res.total += 1
        if _matches(shown, places, unit, cells):
            res.from_sql += 1
        elif _matches(shown, places, unit, facts):
            res.derived += 1
        else:
            if lenient:
                if extra is None:
                    extra = [d for t in cells_by_table if len(t) <= 150 for d in _one_step(t)]
                if _matches(shown, places, unit, extra):
                    res.lenient += 1
                    continue
            res.unmatched.append(raw)
    return res
