"""归因的 Agent 版本（对照实验用，ADR 007）：模型拿着 SQL 工具自己决定查什么、下钻几层、何时停。

和固定下钻流程（attribution.py）拿到的信息相同：同一份表结构、同一份指标口径、同一组可用维度，
也给了同样的分析思路（看超出份额的贡献、查集中度）。区别只在控制流和算术由谁做：
这里由模型自己写每一条 SQL、自己解读结果；固定流程由代码生成 SQL、代码算贡献度。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from metriclens import tools
from metriclens.agent import TOOL_RESULT_MAX_CHARS, _tool
from metriclens.baseline import clean_sql
from metriclens.llm import LLM, LLMResult
from metriclens.schema import enhanced_schema
from metriclens.semantic import Match, SemanticLayer

SYSTEM_TEMPLATE = """You are a senior business analyst. The user asks why a metric changed. \
Investigate with SQL on a read-only SQLite database, then submit the causes.

Tools:
- run_sql: execute one read-only SELECT and see up to 50 rows.
- search_values: find the exact stored value for a filter.
- submit_answer: submit the conclusion and the ranked causes. Call it exactly once to finish.

How to investigate (you decide what to query, how deep to go and when to stop):
- Work out the metric, the period asked about (current) and the period it is compared with \
(base: the one named, or the immediately preceding period of equal length; "同比" = one year \
earlier). Compute the metric in both periods with the company definition below.
- Break the change down by the dimensions listed below. A segment is a cause when its share of \
the change is clearly larger than its share of the metric in the base period; a segment that \
always carries most of the metric is not a cause merely because it carries most of the change.
- Check concentration: whether a few customers, products or single invoices account for the \
change.
- Drill one level further inside the main cause only when that makes the cause more specific.
- Every number in your conclusion must come from a SQL result you ran. Let SQL do the \
arithmetic; do not compute in your head.

submit_answer:
- conclusion: 3-6 sentences in Simplified Chinese for a business owner: the total change, then \
the causes in order of importance with their numbers.
- causes: at most 3, most important first. Each cause is a list of conditions; use more than one \
condition only for a combination (e.g. one country AND one category). `dimension` is one of the \
dimension ids below; `value` is the stored value (country in English as stored, invoice number, \
customer id, stock code, category name as stored).

Today is {today}; the data ends on {as_of}.

{context}

Dimensions (id: column expression, meaning):
{dimensions}

Database schema:
{schema}"""

MAX_ROWS = 50
NUDGE = "Please call submit_answer now with your conclusion and causes."


def _dimension_ids(layer: SemanticLayer) -> list[str]:
    a = layer.attribution
    return [*a.get("dimensions", []), *a.get("entities", []), *filter(None, [a.get("document")])]


def submit_tool(layer: SemanticLayer) -> dict:
    cond = {
        "type": "object",
        "properties": {
            "dimension": {"type": "string", "enum": _dimension_ids(layer)},
            "value": {"type": "string"},
        },
        "required": ["dimension", "value"],
        "additionalProperties": False,
    }
    cause = {
        "type": "object",
        "properties": {"conditions": {"type": "array", "items": cond}},
        "required": ["conditions"],
        "additionalProperties": False,
    }
    return _tool(
        "submit_answer",
        "Submit the final conclusion and the ranked causes.",
        {"conclusion": {"type": "string"}, "causes": {"type": "array", "items": cause}},
    )


def system_prompt(layer: SemanticLayer, role: str | None = None) -> str:
    as_of = date.fromisoformat(layer.attribution["as_of"])
    matches = [Match(m.id, "metric", "synonym", m.name) for m in layer.metrics.values()]
    # 归因会用到的 simple 分量也都在里面；术语一并给出
    matches += [Match(t.id, "term", "synonym", t.name) for t in layer.terms.values()]
    dims = []
    for did in _dimension_ids(layer):
        d = layer.dimensions[did]
        vals = "; ".join(f"{k}={v}" for k, v in list(d.values.items())[:12])
        more = " …" if len(d.values) > 12 else ""
        desc = f" {d.description}" if d.description else ""
        dims.append(
            f"- {d.id}: {d.expr} ({d.name}){desc}" + (f" values: {vals}{more}" if vals else "")
        )
    return SYSTEM_TEMPLATE.format(
        today=(as_of + timedelta(days=1)).isoformat(),
        as_of=as_of.isoformat(),
        context=layer.context(matches, role),
        dimensions="\n".join(dims),
        schema=enhanced_schema(layer.db_id, None, role),
    )


@dataclass
class AgentRun:
    conclusion: str = ""
    causes: list[list[list[str]]] = field(default_factory=list)  # [[[维度, 取值], …], …]
    queries: list[dict] = field(default_factory=list)  # 每条 run_sql：sql / columns / rows / error
    llm_calls: list[LLMResult] = field(default_factory=list)
    steps: int = 0
    submitted: bool = False
    stop_reason: str = ""


class AttributionAgent:
    def __init__(self, max_steps: int = 14):
        self.max_steps = max_steps

    async def __call__(
        self, llm: LLM, layer: SemanticLayer, question: str, role: str | None = None
    ) -> AgentRun:
        db_id = layer.db_id
        system = system_prompt(layer, role)
        agent_tools = [
            _tool(
                "search_values",
                "Fuzzy-search text columns for stored values containing the keyword. "
                "Optionally restrict to one table and column (pass null otherwise).",
                {
                    "keyword": {"type": "string"},
                    "table": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "column": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            ),
            _tool(
                "run_sql",
                f"Execute one read-only SQLite SELECT; returns columns and up to {MAX_ROWS} rows.",
                {"sql": {"type": "string"}},
            ),
            submit_tool(layer),
        ]
        messages: list[dict] = [{"role": "user", "content": f"Question: {question}"}]
        run = AgentRun()
        nudged = False
        for _ in range(self.max_steps):
            res = await llm.complete(system, messages, agent_tools)
            run.llm_calls.append(res)
            run.steps += 1
            run.stop_reason = res.stop_reason
            messages = messages + [{"role": "assistant", "content": res.content}]
            uses = res.tool_uses
            if not uses:
                if res.stop_reason == "refusal" or nudged:
                    run.conclusion = run.conclusion or res.text.strip()
                    break
                nudged = True
                run.conclusion = res.text.strip()
                messages = messages + [{"role": "user", "content": NUDGE}]
                continue
            results = []
            for u in uses:
                name, args = u["name"], u.get("input", {})
                if name == "submit_answer":
                    run.conclusion = str(args.get("conclusion", "")).strip()
                    run.causes = [
                        [
                            [c.get("dimension", ""), str(c.get("value", ""))]
                            for c in cause["conditions"]
                        ]
                        for cause in args.get("causes", [])
                        if cause.get("conditions")
                    ]
                    run.submitted = True
                    out = {"ok": True}
                elif name == "run_sql":
                    sql = clean_sql(args.get("sql", ""))
                    out = await asyncio.to_thread(tools.run_sql, db_id, sql, MAX_ROWS, role)
                    run.queries.append({"sql": sql, **out})
                elif name == "search_values":
                    try:
                        out = await asyncio.to_thread(
                            tools.search_values,
                            db_id,
                            args.get("keyword", ""),
                            args.get("table"),
                            args.get("column"),
                            10,
                            role,
                        )
                    except ValueError as e:
                        out = {"error": "bad_arguments", "message": str(e)}
                else:
                    out = {"error": "unknown_tool", "message": name}
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": u["id"],
                        "content": json.dumps(out, ensure_ascii=False, default=str)[
                            :TOOL_RESULT_MAX_CHARS
                        ],
                    }
                )
            messages = messages + [{"role": "user", "content": results}]
            if run.submitted:
                break
        return run
