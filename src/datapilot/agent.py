"""Agent 版本：模型自主决定何时查取值、何时试跑 SQL，用 submit_answer 交卷。

和固定流程（pipeline.py）拿到的信息相同（增强 schema 在缓存的 system 里），
区别只在控制流：固定流程由代码规则决定何时执行 / 核对 / 纠错，这里交给模型。
db_id 由宿主绑定，不出现在工具参数里，模型无法切换到别的库。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from datapilot import tools
from datapilot.baseline import GenerationResult, clean_sql
from datapilot.llm import LLM
from datapilot.pipeline import user_message
from datapilot.schema import enhanced_schema

SYSTEM_TEMPLATE = """You are a data analyst agent answering questions over a SQLite database.
The schema is below. You have tools:
- search_values: find the exact stored values for a filter (e.g. what is stored for "female").
- run_sql: execute a read-only SELECT and see up to 20 rows (use it to verify your query).
- submit_answer: submit the final SQL. You must call it exactly once to finish.
Use the External Knowledge when provided. Return exactly the columns the question asks for,
in the order asked. Be efficient: explore only what you need.

Database schema:
{schema}"""

NUDGE = "Please call submit_answer with your final SQL now."
TOOL_RESULT_MAX_CHARS = 4000


def _tool(name: str, description: str, props: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

TOOLS = [
    _tool(
        "search_values",
        "Fuzzy-search text columns for stored values containing the keyword. "
        "Optionally restrict to one table and column (pass null otherwise).",
        {"keyword": {"type": "string"}, "table": _NULLABLE_STR, "column": _NULLABLE_STR},
    ),
    _tool(
        "run_sql",
        "Execute one read-only SQLite SELECT and return columns and up to 20 rows, or an error.",
        {"sql": {"type": "string"}},
    ),
    _tool("submit_answer", "Submit the final SQL answer.", {"sql": {"type": "string"}}),
]


class SQLAgent:
    def __init__(self, max_steps: int = 10):
        self.max_steps = max_steps

    async def __call__(
        self, llm: LLM, db_path: str | Path, question: str, evidence: str
    ) -> GenerationResult:
        db_id = Path(db_path).stem
        system = SYSTEM_TEMPLATE.format(schema=enhanced_schema(db_id))
        messages: list[dict] = [{"role": "user", "content": user_message(question, evidence)}]
        calls, trace = [], []
        submitted, last_run_sql = "", ""
        nudged = False
        stop_reason = ""

        for _ in range(self.max_steps):
            res = await llm.complete(system, messages, TOOLS)
            calls.append(res)
            stop_reason = res.stop_reason
            messages = messages + [{"role": "assistant", "content": res.content}]
            if res.text:
                trace.append({"tool": "text", "input": {}, "output": res.text})
            uses = res.tool_uses
            if not uses:
                if res.stop_reason == "refusal" or nudged:
                    break
                nudged = True
                messages = messages + [{"role": "user", "content": NUDGE}]
                continue
            results = []
            for u in uses:
                name, args = u["name"], u.get("input", {})
                if name == "submit_answer":
                    submitted = clean_sql(args.get("sql", ""))
                    out = {"ok": True}
                elif name == "run_sql":
                    last_run_sql = clean_sql(args.get("sql", ""))
                    out = await asyncio.to_thread(tools.run_sql, db_id, last_run_sql, 20)
                elif name == "search_values":
                    try:
                        out = await asyncio.to_thread(
                            tools.search_values,
                            db_id,
                            args.get("keyword", ""),
                            args.get("table"),
                            args.get("column"),
                        )
                    except ValueError as e:
                        out = {"error": "bad_arguments", "message": str(e)}
                else:
                    out = {"error": "unknown_tool", "message": name}
                trace.append({"tool": name, "input": args, "output": out})
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
            if submitted:
                break

        sql = submitted or last_run_sql
        return GenerationResult(
            sql=sql,
            stop_reason=stop_reason,
            llm_calls=calls,
            # Agent 的 run_sql 多是探索查询，不是"纠错前的答案"，不参与改对 / 改错统计
            first_sql=sql,
            trace=trace,
        )

    def input_chars(self, db_path: str | Path, question: str, evidence: str) -> int:
        # 粗估：Agent 平均 3 步，每步都带 system（命中缓存后便宜很多）
        system = SYSTEM_TEMPLATE.format(schema=enhanced_schema(Path(db_path).stem))
        return 3 * (len(system) + len(user_message(question, evidence)))
