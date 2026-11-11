"""数据质量检查（v1.5，ADR 006）：空值、重复、取值范围、外键一致性、业务规则、时间新鲜度。

规则两类：
- 自动：主键重复 / 主键为空、外键一致性（孤儿记录），按库里声明的约束生成，不用配置；
- 配置：`quality_rules/<db_id>.yaml` 里的业务规则（业务键不重复、应有值的列、枚举取值、
  数值范围、行级业务规则、新鲜度）。

报告里只给出问题行的主键（订单号、用户编号等），不带其他列的取值：质量报告给数据负责人看，
不按角色裁剪，所以不能借它看到成本价、客户姓名。接口只对不受限的角色开放。

命令行：
  uv run python -m metriclens.quality shop_ops            # 打印报告
  uv run python -m metriclens.quality shop_ops --json     # JSON
  uv run python -m metriclens.quality shop_ops --as-of 2026-10-01 --strict   # 有 fail 时退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import yaml

from metriclens import config
from metriclens.schema import _sql_literal as _lit
from metriclens.sql_guard import connect_readonly
from metriclens.tools import _q

RULES_DIR = Path(__file__).with_name("quality_rules")
SAMPLE_KEYS = 5
Severity = Literal["fail", "warn"]
Status = Literal["pass", "warn", "fail"]


@dataclass
class Check:
    id: str
    category: Literal["空值", "重复", "取值范围", "外键一致性", "业务规则", "新鲜度"]
    table: str
    description: str
    severity: Severity
    status: Status = "pass"
    failed: int = 0
    total: int = 0
    sample_keys: list = field(default_factory=list)
    detail: str = ""


def load_rules(db_id: str) -> dict:
    path = RULES_DIR / f"{db_id}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


class QualityChecker:
    def __init__(self, db_path: str | Path, rules: dict | None = None):
        self.db_path = Path(db_path)
        self.rules = rules if rules is not None else load_rules(self.db_path.stem)
        self.conn = connect_readonly(self.db_path)
        names = [
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        self.schema = {
            t: self.conn.execute(f"PRAGMA table_info({_q(t)})").fetchall() for t in names
        }

    def close(self) -> None:
        self.conn.close()

    # ---------- 工具 ----------

    def _split(self, column: str) -> tuple[str, str]:
        """规则里的"表.列"必须真实存在：表名、列名会拼进 SQL。"""
        table, _, col = column.partition(".")
        cols = {c[1] for c in self.schema.get(table, [])}
        if col not in cols:
            raise ValueError(f"质量规则引用了不存在的列：{column}")
        return table, col

    def _key(self, table: str) -> str:
        pk = [c[1] for c in sorted(self.schema[table], key=lambda c: c[5]) if c[5]]
        return " || '|' || ".join(_q(c) for c in pk) if pk else "rowid"

    def _violations(self, check: Check, table: str, bad_where: str) -> Check:
        """bad_where 为真的行算失败；统计行数并取几条主键。"""
        t = _q(table)
        check.total = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        check.failed = self.conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {bad_where}").fetchone()[
            0
        ]
        if check.failed:
            check.status = check.severity
            check.sample_keys = [
                r[0]
                for r in self.conn.execute(
                    f"SELECT {self._key(table)} FROM {t} WHERE {bad_where} LIMIT {SAMPLE_KEYS}"
                )
            ]
        return check

    # ---------- 各类检查 ----------

    def primary_keys(self) -> list[Check]:
        out = []
        for table, cols in self.schema.items():
            pk = [c[1] for c in sorted(cols, key=lambda c: c[5]) if c[5]]
            if not pk:
                continue
            key = ", ".join(_q(c) for c in pk)
            ch = Check(f"pk_unique:{table}", "重复", table, f"主键 {', '.join(pk)} 不重复", "fail")
            dup = (
                f"({key}) IN (SELECT {key} FROM {_q(table)} GROUP BY {key} HAVING COUNT(*) > 1)"
                if len(pk) == 1
                else f"EXISTS (SELECT 1 FROM {_q(table)} x WHERE "
                + " AND ".join(f"x.{_q(c)} = {_q(table)}.{_q(c)}" for c in pk)
                + f" AND x.rowid != {_q(table)}.rowid)"
            )
            out.append(self._violations(ch, table, dup))
            ch = Check(
                f"pk_not_null:{table}", "空值", table, f"主键 {', '.join(pk)} 不为空", "fail"
            )
            out.append(self._violations(ch, table, " OR ".join(f"{_q(c)} IS NULL" for c in pk)))
        return out

    def foreign_keys(self) -> list[Check]:
        out = []
        for table in self.schema:
            for fk in self.conn.execute(f"PRAGMA foreign_key_list({_q(table)})").fetchall():
                ref, col, ref_col = fk[2], fk[3], fk[4]
                ch = Check(
                    f"fk:{table}.{col}",
                    "外键一致性",
                    table,
                    f"{table}.{col} 都能在 {ref}.{ref_col} 找到",
                    "fail",
                )
                bad = (
                    f"{_q(col)} IS NOT NULL AND {_q(col)} NOT IN "
                    f"(SELECT {_q(ref_col)} FROM {_q(ref)} WHERE {_q(ref_col)} IS NOT NULL)"
                )
                out.append(self._violations(ch, table, bad))
        return out

    def not_null(self) -> list[Check]:
        out = []
        for r in self.rules.get("not_null", []):
            table, col = self._split(r["column"])
            ch = Check(
                f"not_null:{r['column']}",
                "空值",
                table,
                f"{r['column']} 应有值",
                r.get("severity", "fail"),
            )
            out.append(self._violations(ch, table, f"{_q(col)} IS NULL OR {_q(col)} = ''"))
        return out

    def unique(self) -> list[Check]:
        out = []
        for r in self.rules.get("unique", []):
            parts = [self._split(c) for c in r["columns"]]
            table = parts[0][0]
            if any(t != table for t, _ in parts):
                raise ValueError(f"unique 规则的列要在同一张表：{r['columns']}")
            cols = [c for _, c in parts]
            ch = Check(
                f"unique:{'+'.join(r['columns'])}",
                "重复",
                table,
                r.get("description") or f"{', '.join(r['columns'])} 组合不重复",
                r.get("severity", "fail"),
            )
            t = _q(table)
            same = " AND ".join(f"x.{_q(c)} IS {t}.{_q(c)}" for c in cols)
            bad = f"EXISTS (SELECT 1 FROM {t} x WHERE {same} AND x.rowid != {t}.rowid)"
            out.append(self._violations(ch, table, bad))
        return out

    def accepted_values(self) -> list[Check]:
        out = []
        for r in self.rules.get("accepted_values", []):
            table, col = self._split(r["column"])
            vals = ", ".join(_lit(v) for v in r["values"])
            ch = Check(
                f"values:{r['column']}",
                "取值范围",
                table,
                f"{r['column']} 只取 {', '.join(map(str, r['values']))}",
                r.get("severity", "fail"),
            )
            out.append(
                self._violations(ch, table, f"{_q(col)} IS NOT NULL AND {_q(col)} NOT IN ({vals})")
            )
        return out

    def ranges(self) -> list[Check]:
        out = []
        for r in self.rules.get("ranges", []):
            table, col = self._split(r["column"])
            bad, desc = [], []
            if "min" in r:
                inclusive = r.get("min_inclusive", True)
                bad.append(f"{_q(col)} {'<' if inclusive else '<='} {float(r['min'])}")
                desc.append(f"{'≥' if inclusive else '>'} {r['min']}")
            if "max" in r:
                inclusive = r.get("max_inclusive", True)
                bad.append(f"{_q(col)} {'>' if inclusive else '>='} {float(r['max'])}")
                desc.append(f"{'≤' if inclusive else '<'} {r['max']}")
            ch = Check(
                f"range:{r['column']}",
                "取值范围",
                table,
                f"{r['column']} {' 且 '.join(desc)}",
                r.get("severity", "fail"),
            )
            out.append(self._violations(ch, table, " OR ".join(bad)))
        return out

    def expressions(self) -> list[Check]:
        out = []
        for r in self.rules.get("expressions", []):
            table = r["table"]
            if table not in self.schema:
                raise ValueError(f"质量规则引用了不存在的表：{table}")
            ch = Check(
                f"rule:{r['id']}", "业务规则", table, r["description"], r.get("severity", "fail")
            )
            out.append(self._violations(ch, table, f"NOT ({r['condition']})"))
        return out

    def freshness(self, as_of: date) -> list[Check]:
        out = []
        for r in self.rules.get("freshness", []):
            table, col = self._split(r["column"])
            latest = self.conn.execute(f"SELECT MAX({_q(col)}) FROM {_q(table)}").fetchone()[0]
            limit = int(r["max_age_days"])
            ch = Check(
                f"freshness:{r['column']}",
                "新鲜度",
                table,
                f"{r['column']} 最新数据距检查日期不超过 {limit} 天",
                r.get("severity", "warn"),
            )
            ch.total = 1
            try:
                latest_day = None if latest is None else date.fromisoformat(str(latest)[:10])
            except ValueError:
                latest_day = None
            if latest is None:
                ch.status, ch.failed, ch.detail = ch.severity, 1, "表里没有数据"
            elif latest_day is None:
                ch.status, ch.failed, ch.detail = ch.severity, 1, f"无法解析时间：{latest}"
            else:
                age = (as_of - latest_day).days
                ch.detail = f"最新 {latest}，距 {as_of.isoformat()} {age} 天"
                if age > limit:
                    ch.status, ch.failed = ch.severity, 1
                elif age < 0:  # 数据晚于检查日期：时钟或时区有问题，或检查日期选错
                    ch.status, ch.failed = ch.severity, 1
                    ch.detail += "（数据晚于检查日期）"
            out.append(ch)
        return out

    def run(self, as_of: date | None = None) -> list[Check]:
        as_of = as_of or date.today()
        return (
            self.primary_keys()
            + self.unique()
            + self.not_null()
            + self.accepted_values()
            + self.ranges()
            + self.foreign_keys()
            + self.expressions()
            + self.freshness(as_of)
        )


def report(db_id: str, as_of: date | None = None, db_path: str | Path | None = None) -> dict:
    as_of = as_of or date.today()
    checker = QualityChecker(db_path or config.db_path(db_id), load_rules(db_id))
    try:
        checks = checker.run(as_of)
    finally:
        checker.close()
    counts = {s: sum(c.status == s for c in checks) for s in ("pass", "warn", "fail")}
    return {
        "db_id": db_id,
        "as_of": as_of.isoformat(),
        "summary": {**counts, "total": len(checks)},
        "checks": [asdict(c) for c in checks],
    }


def render(rep: dict) -> str:
    s = rep["summary"]
    lines = [
        f"{rep['db_id']} 数据质量（检查日期 {rep['as_of']}）：{s['total']} 项，"
        f"通过 {s['pass']}，警告 {s['warn']}，失败 {s['fail']}"
    ]
    mark = {"pass": "✓", "warn": "!", "fail": "✗"}
    for c in rep["checks"]:
        extra = f"  {c['failed']}/{c['total']} 行" if c["failed"] else ""
        extra += f"  例：{', '.join(map(str, c['sample_keys']))}" if c["sample_keys"] else ""
        extra += f"  （{c['detail']}）" if c["detail"] else ""
        lines.append(f"{mark[c['status']]} [{c['category']}] {c['description']}{extra}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("db_id")
    p.add_argument("--as-of", help="检查日期 YYYY-MM-DD（新鲜度按它算，默认今天）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict", action="store_true", help="有 fail 级问题时退出码为 1")
    args = p.parse_args(argv)
    if not config.db_path(args.db_id).exists():
        print(f"找不到数据库 {args.db_id}", file=sys.stderr)
        return 2
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    rep = report(args.db_id, as_of)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else render(rep))
    return 1 if args.strict and rep["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
