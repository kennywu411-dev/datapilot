"""指标语义层（v1.5，ADR 006）：把公司口径沉淀成指标字典，提问时自动带上，不靠用户写 evidence。

定义文件 `metrics/<db_id>.yaml`：全局约定、业务术语（过滤条件）、指标、维度、表关联。
两种用法，都包在现有固定流程外面（与 RoutedPipeline 同样的包装方式）：
- context（口径注入）：同义词最长匹配 + 字二元组模糊检索兜底，找到问题涉及的指标和术语，
  把口径和计算片段并入 External Knowledge，模型照常写 SQL；
- compile（语义层直出）：先让模型把问题映射成"指标 + 维度 + 过滤 + 时间 + 排序"，
  由语义层拼 SQL；映射不了（比率、同比、窗口等）或执行出错时退回 context。

行列级权限：指标用到当前角色看不到的列时，context 不给出计算片段、只说明无权访问；
compile 直接拒绝。语义层生成的 SQL 照样经过 rls 改写和授权回调，不会多出一条绕过路径。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

import sqlglot
import yaml
from pydantic import BaseModel, Field
from sqlglot import exp

from datapilot import rls
from datapilot.baseline import GenerationResult
from datapilot.llm import LLM
from datapilot.sql_guard import SQLRejected, run_readonly, table_columns

METRICS_DIR = Path(__file__).with_name("metrics")
FUZZY_MIN_COVERAGE = 0.6  # 兜底检索：同义词的字二元组至少有这么多出现在问题里，否则宁可不匹配
EXEC_TIMEOUT_S = 30.0
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NotCompilable(Exception):
    """问题映射不到语义层能直接生成的查询（退回口径注入）。"""


@dataclass(frozen=True)
class Term:
    id: str
    name: str
    description: str
    synonyms: tuple[str, ...] = ()
    sql: str = ""  # 过滤条件
    reference_sql: str = ""  # 写不成单个条件的术语（如首单）给参考 SQL


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    kind: Literal["simple", "derived"]
    description: str = ""
    synonyms: tuple[str, ...] = ()
    base: str = ""
    expr: str = ""
    filters: tuple[str, ...] = ()  # 引用 terms
    filters_sql: tuple[str, ...] = ()  # 指标自带、不值得单列为术语的条件
    reference_sql: str = ""


@dataclass(frozen=True)
class Dimension:
    id: str
    name: str
    expr: str  # {time} 占位符替换成主表的时间列
    synonyms: tuple[str, ...] = ()
    description: str = ""
    values: dict[str, str] = field(default_factory=dict)  # 存储值 → 中文含义


@dataclass(frozen=True)
class Match:
    id: str
    kind: Literal["metric", "term"]
    via: Literal["synonym", "fuzzy"]
    text: str  # 命中的同义词，或模糊检索命中的同义词和覆盖率


class DimFilter(BaseModel):
    dimension: str = Field(description="Dimension id from the catalog.")
    values: list[str] = Field(description="Stored values to keep (codes, not labels).")


class SemanticQuery(BaseModel):
    """模型把问题映射到指标字典的结果；mappable=false 表示语义层表达不了。"""

    mappable: bool
    metric: str = ""
    group_by: list[str] = Field(default_factory=list)
    filters: list[DimFilter] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list, description="Extra term ids used as filters.")
    time_from: str = Field("", description="Inclusive start date YYYY-MM-DD, or empty.")
    time_to: str = Field("", description="Inclusive end date YYYY-MM-DD, or empty.")
    order: Literal["", "desc", "asc"] = ""
    limit: int = 0
    output_metric: bool = True
    round: int = -1


def _columns(fragment: str, *, strict: bool) -> set[tuple[str, str]]:
    """SQL 片段用到的 (表, 列)。strict 时要求每个列都写成"表名.列名"。"""
    text = fragment.replace("{time}", "NULL")
    try:
        tree = sqlglot.parse_one(
            text if text.lstrip().upper().startswith(("SELECT", "WITH")) else f"SELECT {text}",
            read="sqlite",
        )
    except sqlglot.errors.SqlglotError as e:
        raise ValueError(f"无法解析 SQL 片段：{fragment}（{e}）") from None
    cols = set()
    for c in tree.find_all(exp.Column):
        if isinstance(c.this, exp.Star):
            continue
        if not c.table:
            if strict:
                raise ValueError(f"列要写成 表名.列名：{c.sql()}（片段：{fragment}）")
            continue  # 参考 SQL 里的别名（如 n、rn）
        cols.add((c.table.lower(), c.name.lower()))
    return cols


def _quote(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _bigrams(text: str) -> list[str]:
    s = re.sub(r"\s+", "", text.lower())
    return [s[i : i + 2] for i in range(len(s) - 1)] or ([s] if s else [])


class SemanticLayer:
    def __init__(
        self,
        db_id: str,
        conventions: list[str],
        terms: dict[str, Term],
        metrics: dict[str, Metric],
        dimensions: dict[str, Dimension],
        time_columns: dict[str, str],
        joins: list[tuple[str, str, str]],
    ):
        self.db_id = db_id
        self.conventions = conventions
        self.terms = terms
        self.metrics = metrics
        self.dimensions = dimensions
        self.time_columns = time_columns
        self.joins = joins
        self._check_refs()
        # 同义词按长度倒序：最长匹配，"有效订单数"不会再拆出"订单数"
        entries = [(s, mid, "metric") for mid, m in metrics.items() for s in (m.name, *m.synonyms)]
        entries += [(s, tid, "term") for tid, t in terms.items() for s in (t.name, *t.synonyms)]
        self._synonyms = sorted(
            {(s.lower().replace(" ", ""), i, k) for s, i, k in entries if s},
            key=lambda x: -len(x[0]),
        )

    # ---------- 加载与校验 ----------

    @classmethod
    def from_dict(cls, d: dict) -> SemanticLayer:
        def tup(v):
            return tuple(v or ())

        terms = {
            k: Term(
                k,
                v["name"],
                v.get("description", ""),
                tup(v.get("synonyms")),
                v.get("sql", ""),
                v.get("reference_sql", ""),
            )
            for k, v in (d.get("terms") or {}).items()
        }
        metrics = {
            k: Metric(
                k,
                v["name"],
                v.get("kind", "simple"),
                v.get("description", ""),
                tup(v.get("synonyms")),
                v.get("base", ""),
                v.get("expr", ""),
                tup(v.get("filters")),
                tup(v.get("filters_sql")),
                v.get("reference_sql", ""),
            )
            for k, v in (d.get("metrics") or {}).items()
        }
        dims = {
            k: Dimension(
                k,
                v["name"],
                v["expr"],
                tup(v.get("synonyms")),
                v.get("description", ""),
                {str(a): str(b) for a, b in (v.get("values") or {}).items()},
            )
            for k, v in (d.get("dimensions") or {}).items()
        }
        joins = [(a.lower(), b.lower(), cond) for a, b, cond in d.get("joins") or []]
        return cls(
            d["db_id"],
            list(d.get("conventions") or []),
            terms,
            metrics,
            dims,
            {k.lower(): v for k, v in (d.get("time_columns") or {}).items()},
            joins,
        )

    def _check_refs(self) -> None:
        """加载时就拦住写错的定义：列不带表名、引用不存在的术语、simple 指标缺主表。"""
        for m in self.metrics.values():
            if m.kind not in ("simple", "derived"):
                raise ValueError(f"指标 {m.id} 的 kind 只能是 simple / derived")
            if m.kind == "simple" and not (m.base and m.expr):
                raise ValueError(f"simple 指标 {m.id} 要有 base 和 expr")
            unknown = [f for f in m.filters if f not in self.terms or not self.terms[f].sql]
            if unknown:
                raise ValueError(f"指标 {m.id} 引用了不存在或没有条件的术语：{unknown}")
            self.columns_of_metric(m)
        for t in self.terms.values():
            _columns(t.sql, strict=True) if t.sql else None
        for dim in self.dimensions.values():
            _columns(dim.expr, strict=True)

    def columns_of_metric(self, m: Metric) -> set[tuple[str, str]]:
        cols = set()
        for frag in (m.expr, *m.filters_sql, *(self.terms[f].sql for f in m.filters)):
            if frag:
                cols |= _columns(frag, strict=True)
        if m.reference_sql:
            cols |= _columns(m.reference_sql, strict=False)
        return cols

    def columns_of_term(self, t: Term) -> set[tuple[str, str]]:
        cols = _columns(t.sql, strict=True) if t.sql else set()
        if t.reference_sql:
            cols |= _columns(t.reference_sql, strict=False)
        return cols

    def validate(self, db_path: str | Path) -> list[str]:
        """对照真实库检查：列都存在、simple 指标能执行、参考 SQL 能执行。返回错误列表。"""
        real = {t.lower(): {c.lower() for c in cs} for t, cs in table_columns(str(db_path)).items()}
        errors = []
        frags = [(f"指标 {m.id}", self.columns_of_metric(m)) for m in self.metrics.values()]
        frags += [(f"术语 {t.id}", self.columns_of_term(t)) for t in self.terms.values()]
        frags += [(f"维度 {d.id}", _columns(d.expr, strict=True)) for d in self.dimensions.values()]
        frags += [("时间列", _columns(c, strict=True)) for c in self.time_columns.values()]
        for where, cols in frags:
            for t, c in sorted(cols):
                if c not in real.get(t, set()):
                    errors.append(f"{where}：{t}.{c} 不存在")
        if errors:
            return errors
        sqls = [
            (f"指标 {m.id}", self.compile(SemanticQuery(mappable=True, metric=m.id)))
            for m in self.metrics.values()
            if m.kind == "simple"
        ]
        sqls += [
            (f"指标 {m.id}", m.reference_sql) for m in self.metrics.values() if m.reference_sql
        ]
        sqls += [(f"术语 {t.id}", t.reference_sql) for t in self.terms.values() if t.reference_sql]
        for d in self.dimensions.values():
            base = next((m for m in self.metrics.values() if m.kind == "simple"), None)
            if base is not None and self._reachable(base.base, d):
                sqls.append(
                    (
                        f"维度 {d.id}",
                        self.compile(SemanticQuery(mappable=True, metric=base.id, group_by=[d.id])),
                    )
                )
        for where, sql in sqls:
            try:
                run_readonly(db_path, sql, max_rows=1)
            except (SQLRejected, sqlite3.Error, TimeoutError) as e:
                errors.append(f"{where} 执行失败：{e}")
        return errors

    # ---------- 匹配 ----------

    def match(self, question: str) -> list[Match]:
        """同义词最长匹配（不重叠）；一个指标都没命中时做模糊检索兜底：
        按"同义词的字二元组有多少出现在问题里"打分，取覆盖率最高且达到阈值的一个指标。
        不用 BM25：指标只有十几个、同义词很短，IDF 不稳定，"多少""订单"这类常见二元组会误命中。"""
        text = question.lower().replace(" ", "")
        taken = [False] * len(text)
        hits: list[tuple[int, Match]] = []
        seen = set()
        for syn, id_, kind in self._synonyms:
            start = text.find(syn)
            while start != -1:
                end = start + len(syn)
                if not any(taken[start:end]):
                    taken[start:end] = [True] * len(syn)
                    if (kind, id_) not in seen:
                        seen.add((kind, id_))
                        hits.append((start, Match(id_, kind, "synonym", syn)))
                start = text.find(syn, start + 1)
        found = [m for _, m in sorted(hits, key=lambda x: x[0])]
        if not any(m.kind == "metric" for m in found):
            # 只在没被精确命中的文字上检索："有效订单"已命中术语，不再模糊成"有效订单数"
            rest = "".join(ch if not t else " " for ch, t in zip(text, taken, strict=True))
            best = self._fuzzy(rest)
            if best is not None:
                found.append(best)
        return found

    def _fuzzy(self, text: str) -> Match | None:
        grams = {g for part in text.split() for g in _bigrams(part)}
        best, best_cov = None, FUZZY_MIN_COVERAGE
        for syn, id_, kind in self._synonyms:
            sg = set(_bigrams(syn))
            if kind != "metric" or len(sg) < 2:  # 两个字的同义词只做精确匹配
                continue
            cov = len(sg & grams) / len(sg)
            if cov >= best_cov and (best is None or cov > best_cov):
                best, best_cov = Match(id_, "metric", "fuzzy", f"{syn} {cov:.2f}"), cov
        return best

    # ---------- 口径注入 ----------

    def _hidden(self, cols: set[tuple[str, str]], role: str | None) -> list[str]:
        policy = rls.policy_for(self.db_id, role)
        if policy is None:
            return []
        return sorted(f"{t}.{c}" for t, c in cols if c in policy.hidden_in(t))

    def _where(self, m: Metric) -> list[str]:
        return [self.terms[f].sql for f in m.filters] + list(m.filters_sql)

    def context(self, matches: list[Match], role: str | None = None) -> str:
        """口径说明（中文），并入 External Knowledge。全局约定每次都带。"""
        lines = []
        for mt in matches:
            if mt.kind == "metric":
                m = self.metrics[mt.id]
                hidden = self._hidden(self.columns_of_metric(m), role)
                if hidden:
                    lines.append(
                        f"- {m.name}：当前角色无权访问该指标依赖的 {', '.join(hidden)}，不能计算"
                    )
                    continue
                line = f"- {m.name}（公司口径）：{m.description}"
                if m.kind == "simple":
                    where = " AND ".join(self._where(m))
                    line += f"。计算：{m.expr}" + (f"，条件：{where}" if where else "")
                if m.reference_sql:
                    line += f"。参考 SQL：{m.reference_sql}"
                lines.append(line)
            else:
                t = self.terms[mt.id]
                if self._hidden(self.columns_of_term(t), role):
                    continue
                line = f"- {t.name}（公司口径）：{t.description}"
                line += f"。条件：{t.sql}" if t.sql else ""
                line += f"。参考 SQL：{t.reference_sql}" if t.reference_sql else ""
                lines.append(line)
        parts = []
        if lines:
            parts.append(
                "指标与术语口径（按公司口径计算，不要自行换成其他定义）：\n" + "\n".join(lines)
            )
        if self.conventions:
            parts.append("通用约定：\n" + "\n".join(f"- {c}" for c in self.conventions))
        return "\n".join(parts) if parts else ""

    # ---------- 语义层直出 ----------

    def catalog(self) -> str:
        """给映射模型看的指标字典（英文说明 + 中文名称）。"""
        out = ["Metrics (filters are applied automatically):"]
        for m in self.metrics.values():
            if m.kind == "simple":
                out.append(f"- {m.id} ({m.name}): {m.description}")
            else:
                out.append(
                    f"- {m.id} ({m.name}): {m.description} [NOT supported, set mappable=false]"
                )
        out.append("Dimensions (group_by / filters):")
        for d in self.dimensions.values():
            vals = ", ".join(f"{k}={v}" for k, v in d.values.items())
            desc = f" {d.description}" if d.description else ""
            out.append(f"- {d.id} ({d.name}){desc}" + (f" values: {vals}" if vals else ""))
        terms = [t for t in self.terms.values() if t.sql]
        if terms:
            out.append("Terms (extra filters):")
            out += [f"- {t.id} ({t.name}): {t.description}" for t in terms]
        return "\n".join(out)

    def _reachable(self, base: str, d: Dimension) -> bool:
        tables = {t for t, _ in _columns(d.expr, strict=True)}
        return tables <= set(self._join_tree(base))

    def _join_tree(self, base: str) -> dict[str, tuple[str, str] | None]:
        """从主表出发 BFS：表 → (父表, 关联条件)，按发现顺序。"""
        tree: dict[str, tuple[str, str] | None] = {base: None}
        frontier = [base]
        while frontier:
            nxt = []
            for cur in frontier:
                for a, b, cond in self.joins:
                    other = b if a == cur else a if b == cur else None
                    if other and other not in tree:
                        tree[other] = (cur, cond)
                        nxt.append(other)
            frontier = nxt
        return tree

    def compile(self, q: SemanticQuery, role: str | None = None) -> str:
        if not q.mappable:
            raise NotCompilable("问题映射不到指标字典")
        m = self.metrics.get(q.metric)
        if m is None:
            raise NotCompilable(f"没有指标 {q.metric}")
        if m.kind != "simple":
            raise NotCompilable(f"{m.name} 是复合指标，语义层不直接生成 SQL")
        time_col = self.time_columns.get(m.base, "")
        dims = []
        for did in q.group_by:
            if did not in self.dimensions:
                raise NotCompilable(f"没有维度 {did}")
            dims.append(self.dimensions[did])
        filters = []
        for f in q.filters:
            if f.dimension not in self.dimensions or not f.values:
                raise NotCompilable(f"过滤维度不对：{f.dimension}")
            filters.append((self.dimensions[f.dimension], f.values))
        terms = []
        for tid in q.terms:
            if tid not in self.terms or not self.terms[tid].sql:
                raise NotCompilable(f"没有可用作条件的术语 {tid}")
            terms.append(self.terms[tid])
        for d in (q.time_from, q.time_to):
            if d and not _DATE_RE.match(d):
                raise NotCompilable(f"日期格式不对：{d}")
        if (q.time_from or q.time_to or any("{time}" in d.expr for d in dims)) and not time_col:
            raise NotCompilable(f"{m.base} 没有时间列")
        if not q.output_metric and not dims:
            raise NotCompilable("不输出指标值时至少要有一个维度")

        def expr_of(d: Dimension) -> str:
            return d.expr.replace("{time}", time_col)

        cols = self.columns_of_metric(m)
        for d in [*dims, *(d for d, _ in filters)]:
            cols |= _columns(expr_of(d), strict=True)
        for t in terms:
            cols |= _columns(t.sql, strict=True)
        hidden = self._hidden(cols, role)
        if hidden:
            raise rls.AccessDenied(f"当前角色无权访问 {', '.join(hidden)}")

        tree = self._join_tree(m.base)
        need = {t for t, _ in cols} | {m.base}
        if not need <= set(tree):
            raise NotCompilable(f"无法从 {m.base} 关联到 {sorted(need - set(tree))}")
        closure = set()
        for t in need:
            while t is not None and t not in closure:
                closure.add(t)
                t = tree[t][0] if tree[t] else None
        joins = [f"JOIN {t} ON {tree[t][1]}" for t in tree if t in closure and tree[t]]

        where = self._where(m) + [t.sql for t in terms]
        where += [f"{expr_of(d)} IN ({', '.join(_quote(v) for v in vals)})" for d, vals in filters]
        if q.time_from:
            where.append(f"{time_col} >= {_quote(q.time_from)}")
        if q.time_to:
            where.append(f"{time_col} <= {_quote(q.time_to + ' 23:59:59')}")
        value = f"ROUND({m.expr}, {q.round})" if q.round >= 0 else m.expr
        select = [expr_of(d) for d in dims] + ([value] if q.output_metric else [])
        sql = f"SELECT {', '.join(select)} FROM {m.base}"
        sql += "".join(f" {j}" for j in joins)
        if where:
            sql += " WHERE " + " AND ".join(f"({w})" for w in where)
        if dims:
            sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims)))
        if q.order:
            sql += f" ORDER BY {m.expr} {q.order.upper()}"
        if q.limit > 0:
            sql += f" LIMIT {int(q.limit)}"
        return sql


@cache
def load_layer(db_id: str) -> SemanticLayer | None:
    path = METRICS_DIR / f"{db_id}.yaml"
    if not path.exists():
        return None
    return SemanticLayer.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


# ---------- 流程接入 ----------

PLAN_SYSTEM = """You map a business question onto a metric catalog so that SQL can be generated \
from the catalog instead of written by hand.
Set mappable=true only if the question is answered by ONE simple metric from the catalog, \
optionally grouped by dimensions, filtered by dimension values or terms, restricted to a time \
range, sorted and limited. Set mappable=false for anything else: ratios or differences between \
metrics, period-over-period comparisons, top-N within each group, window logic, lookups that \
need no metric, or metrics marked NOT supported.
Field rules:
- time_from / time_to: inclusive dates YYYY-MM-DD on the metric's time column; empty if no range.
- filters: use stored values listed in the catalog (codes, not labels); for free-text \
dimensions (city, brand, product, campaign) use the exact name as it appears in the question.
- Output columns are the group_by dimensions in order, then the metric value. Set \
output_metric=false when the question asks only for the dimension (e.g. "which brand").
- order / limit for "most / highest / top N" questions; round = decimals if the question \
asks for them, else -1.

Catalog:
{catalog}"""


class SemanticPipeline:
    """包在固定流程外面：mode="context" 口径注入；mode="compile" 语义层直出，映射不了退回注入。
    库没有指标字典时原样交给内层流程。"""

    def __init__(self, base, mode: Literal["context", "compile"] = "context"):
        if mode not in ("context", "compile"):
            raise ValueError("mode 只能是 context / compile")
        self.base = base
        self.mode = mode

    def _prepare(self, db_path: Path, question: str, evidence: str, role: str | None):
        layer = load_layer(db_path.stem)
        if layer is None:
            return None, [], evidence
        matches = layer.match(question)
        ctx = layer.context(matches, role)
        return layer, matches, "\n".join(p for p in (evidence.strip(), ctx) if p)

    async def astream(
        self, llm: LLM, db_path: str | Path, question: str, evidence: str, role: str | None = None
    ):
        db_path = Path(db_path)
        layer, matches, merged = self._prepare(db_path, question, evidence, role)
        trace = []
        calls = []
        if layer is not None:
            step = {"step": "semantic", "matched": [m.id for m in matches]}
            trace.append(step)
            names = [
                (layer.metrics[m.id] if m.kind == "metric" else layer.terms[m.id]).name
                for m in matches
            ]
            yield "semantic", {"matched": [m.id for m in matches], "names": names}
        if layer is not None and self.mode == "compile":
            out = await self._try_compile(layer, llm, db_path, question, matches, role)
            plan_res, compiled = out[0], out[1]
            calls.append(plan_res)
            trace.append(out[2])
            yield "plan", {k: v for k, v in out[2].items() if k != "step"}
            if compiled is not None:
                compiled.llm_calls = calls
                compiled.trace = trace
                yield "result", compiled
                return
        async for node, payload in self.base.astream(llm, db_path, question, merged, role):
            if node == "result":
                payload.llm_calls = calls + payload.llm_calls
                payload.trace = trace + payload.trace
            yield node, payload

    async def _try_compile(self, layer, llm, db_path, question, matches, role):
        hint = ", ".join(m.id for m in matches)
        user = f"Question: {question}" + (
            f"\nLikely relevant catalog entries: {hint}" if hint else ""
        )
        res = await llm.complete(
            PLAN_SYSTEM.format(catalog=layer.catalog()),
            [{"role": "user", "content": user}],
            [],
            output_format=SemanticQuery,
        )
        plan = res.parsed if isinstance(res.parsed, SemanticQuery) else None
        step = {"step": "plan", "plan": plan.model_dump() if plan else None, "compiled": False}
        if plan is None or not plan.mappable:
            return res, None, step
        try:
            sql = layer.compile(plan, role)
        except NotCompilable as e:
            step["error"] = str(e)
            return res, None, step
        except rls.AccessDenied as e:
            col = re.search(r"无权访问 ([\w.]+)", str(e))
            sql = f"SELECT 'NO_PERMISSION: {col.group(1) if col else 'restricted'}' AS message"
        try:
            r = await asyncio.to_thread(
                run_readonly,
                db_path,
                sql,
                timeout_s=EXEC_TIMEOUT_S,
                max_rows=self.base.cfg.result_rows,
                policy=rls.policy_for(db_path.stem, role),
            )
        except (SQLRejected, sqlite3.Error, TimeoutError) as e:
            step["error"] = str(e)
            return res, None, step
        step.update(compiled=True, sql=sql)
        result = GenerationResult(
            sql=sql,
            stop_reason=res.stop_reason,
            first_sql=sql,
            columns=r.columns,
            rows=r.rows,
            truncated=r.truncated,
        )
        return res, result, step

    async def __call__(
        self, llm: LLM, db_path: str | Path, question: str, evidence: str, role: str | None = None
    ) -> GenerationResult:
        result = None
        async for node, payload in self.astream(llm, db_path, question, evidence, role):
            if node == "result":
                result = payload
        return result

    def input_chars(
        self, db_path: str | Path, question: str, evidence: str, role: str | None = None
    ) -> int:
        db_path = Path(db_path)
        layer, _, merged = self._prepare(db_path, question, evidence, role)
        n = self.base.input_chars(db_path, question, merged, role)
        if layer is not None and self.mode == "compile":
            n += len(PLAN_SYSTEM) + len(layer.catalog()) + len(question)
        return n
