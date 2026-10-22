"""D2 固定流程（LangGraph 状态机）：generate → execute → check →（correct → execute …）→ END。

每一项改进都是 PipelineConfig 上的开关，便于逐项对比实验：
- schema="ddl" | "enhanced"：官方 DDL，或附 BIRD 列说明 / 取值说明 / 示例值
- retrieval_k：>0 时只放 BM25 检索到的 top-k 表 + 外键邻表（消融）
- value_check：核对 SQL 里的文本字面量是否真实存在
- max_corrections：报错 / 空结果 / 值不存在时带反馈重写的最大次数

schema 放在 system 并打缓存断点（修复 P4），问题和 evidence 放 user。
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from datapilot.baseline import GenerationResult, SQLAnswer, clean_sql
from datapilot.llm import LLM, LLMResult
from datapilot.schema import ddl_for, enhanced_schema, retrieve_tables, schema_ddl
from datapilot.sql_guard import SQLRejected, run_readonly
from datapilot.value_check import check_values

SYSTEM_TEMPLATE = """You are an expert in SQLite text-to-SQL.
Given the database schema below, write one SQLite SELECT query that answers the user's question.
Use the External Knowledge when it is provided; it defines terms and formulas used by the question.
Return exactly the columns the question asks for, in the order asked. Think step by step, then put \
only the final SQL in the `sql` field.

Database schema:
{schema}"""

EXEC_TIMEOUT_S = 30.0
PREVIEW_ROWS = 5


@dataclass(frozen=True)
class PipelineConfig:
    schema: Literal["ddl", "enhanced"] = "ddl"
    retrieval_k: int = 0
    value_check: bool = False
    max_corrections: int = 0


class State(TypedDict, total=False):
    messages: list[dict]
    sql: str
    prev_sql: str
    first_sql: str
    stop_reason: str
    exec_error: str
    rows: list
    issues: list[str]
    corrections: int
    llm_calls: list[LLMResult]
    trace: list[dict]


def build_system(cfg: PipelineConfig, db_path: Path, question: str, evidence: str) -> str:
    db_id = Path(db_path).stem
    if cfg.retrieval_k > 0:
        tables = retrieve_tables(db_id, question, evidence, cfg.retrieval_k)
        schema = (
            enhanced_schema(db_id, tuple(tables))
            if cfg.schema == "enhanced"
            else ddl_for(db_id, tables)
        )
    else:
        schema = enhanced_schema(db_id) if cfg.schema == "enhanced" else schema_ddl(str(db_path))
    return SYSTEM_TEMPLATE.format(schema=schema)


def user_message(question: str, evidence: str) -> str:
    knowledge = evidence.strip()
    return f"-- Question: {question}" + (
        f"\n-- External Knowledge: {knowledge}" if knowledge else ""
    )


def feedback_message(state: State) -> str:
    parts = []
    if state.get("exec_error"):
        parts.append(f"The SQL failed to execute: {state['exec_error']}")
    elif state.get("rows") == []:
        parts.append(
            "The SQL executed successfully but returned 0 rows. If the question expects results, "
            "re-check joins, filters and literal values. If an empty result is genuinely correct, "
            "return the same SQL."
        )
    value_issues = [i for i in state.get("issues", []) if i.startswith("Column ")]
    if value_issues:
        parts.append(
            "Value check: "
            + " ".join(value_issues)
            + " Use the exact stored values. If your SQL is already correct, return it unchanged."
        )
    return "\n".join(parts) + "\nReturn the corrected SQL in the `sql` field."


class FixedPipeline:
    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg

    async def __call__(
        self, llm: LLM, db_path: str | Path, question: str, evidence: str
    ) -> GenerationResult:
        cfg, db_path = self.cfg, Path(db_path)
        db_id = db_path.stem
        system = build_system(cfg, db_path, question, evidence)

        async def ask(state: State, messages: list[dict]) -> State:
            res = await llm.complete(system, messages, [], output_format=SQLAnswer)
            sql = clean_sql(res.parsed.sql) if res.parsed else ""
            return {
                "messages": messages + [{"role": "assistant", "content": res.content}],
                "prev_sql": state.get("sql", ""),
                "sql": sql,
                "stop_reason": res.stop_reason,
                "llm_calls": state.get("llm_calls", []) + [res],
            }

        async def generate(state: State) -> State:
            out = await ask(state, [{"role": "user", "content": user_message(question, evidence)}])
            out["first_sql"] = out["sql"]
            out["trace"] = [{"step": "generate", "sql": out["sql"]}]
            return out

        async def execute(state: State) -> State:
            try:
                res = await asyncio.to_thread(
                    run_readonly,
                    db_path,
                    state["sql"],
                    timeout_s=EXEC_TIMEOUT_S,
                    max_rows=PREVIEW_ROWS,
                )
                return {"exec_error": "", "rows": res.rows}
            except (SQLRejected, TimeoutError, sqlite3.Error) as e:
                return {"exec_error": str(e), "rows": None}

        async def check(state: State) -> State:
            issues = []
            if state["exec_error"]:
                issues.append(f"error: {state['exec_error']}")
            elif state["rows"] == []:
                issues.append("empty result")
            if cfg.value_check and not state["exec_error"]:
                issues += await asyncio.to_thread(check_values, db_id, state["sql"])
            trace = state["trace"] + [{"step": "check", "issues": issues, "rows": state["rows"]}]
            return {"issues": issues, "trace": trace}

        async def correct(state: State) -> State:
            messages = state["messages"] + [{"role": "user", "content": feedback_message(state)}]
            out = await ask(state, messages)
            out["corrections"] = state.get("corrections", 0) + 1
            out["trace"] = state["trace"] + [{"step": "correct", "sql": out["sql"]}]
            return out

        def after_generate(state: State) -> str:
            return "execute" if state["sql"] else END

        def after_check(state: State) -> str:
            if not state["issues"] or state.get("corrections", 0) >= cfg.max_corrections:
                return END
            return "correct"

        def after_correct(state: State) -> str:
            # 没有 SQL，或模型返回同一条 SQL（确认原答案正确），不再循环
            if not state["sql"] or state["sql"] == state["prev_sql"]:
                return END
            return "execute"

        g = StateGraph(State)
        for name, fn in [
            ("generate", generate),
            ("execute", execute),
            ("check", check),
            ("correct", correct),
        ]:
            g.add_node(name, fn)
        g.add_edge(START, "generate")
        g.add_conditional_edges("generate", after_generate, ["execute", END])
        g.add_edge("execute", "check")
        g.add_conditional_edges("check", after_check, ["correct", END])
        g.add_conditional_edges("correct", after_correct, ["execute", END])
        final = await g.compile().ainvoke({"corrections": 0, "llm_calls": [], "trace": []})

        return GenerationResult(
            sql=final.get("sql", ""),
            stop_reason=final.get("stop_reason", ""),
            llm_calls=final.get("llm_calls", []),
            first_sql=final.get("first_sql", ""),
            trace=final.get("trace", []),
        )

    def input_chars(self, db_path: str | Path, question: str, evidence: str) -> int:
        return len(build_system(self.cfg, Path(db_path), question, evidence)) + len(
            user_message(question, evidence)
        )
