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

from datapilot import rls
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
    result_rows: int = PREVIEW_ROWS  # 执行时取多少行；服务层设成展示上限，结果直接复用


class Ctx(TypedDict):
    """每题不变的上下文，随 state 传入，让图只需编译一次。"""

    llm: LLM
    system: str
    db_path: Path
    db_id: str
    user: str
    max_rows: int
    role: str | None  # 行列级权限的角色（v1.4）；None 不受限


class State(TypedDict, total=False):
    ctx: Ctx
    messages: list[dict]
    sql: str
    prev_sql: str
    first_sql: str
    stop_reason: str
    exec_error: str
    rows: list
    columns: list[str]
    truncated: bool
    issues: list[str]
    corrections: int
    llm_calls: list[LLMResult]
    trace: list[dict]


def build_system(
    cfg: PipelineConfig, db_path: Path, question: str, evidence: str, role: str | None = None
) -> str:
    db_id = Path(db_path).stem
    policy = rls.policy_for(db_id, role)
    if policy is not None:
        # 受限角色只支持增强 schema：官方 DDL 是原样建表语句，会把无权访问的列和说明一起给出去
        if cfg.schema != "enhanced":
            raise ValueError("受限角色只支持 schema='enhanced'")
        tables = None
        if cfg.retrieval_k > 0:
            tables = tuple(retrieve_tables(db_id, question, evidence, cfg.retrieval_k))
        schema = enhanced_schema(db_id, tables, role)
        note = "\n\n" + policy.prompt_note() if rls.PROMPT_NOTE else ""
        return SYSTEM_TEMPLATE.format(schema=schema) + note
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


async def _ask(state: State, messages: list[dict]) -> State:
    ctx = state["ctx"]
    res = await ctx["llm"].complete(ctx["system"], messages, [], output_format=SQLAnswer)
    sql = clean_sql(res.parsed.sql) if res.parsed else ""
    return {
        "messages": messages + [{"role": "assistant", "content": res.content}],
        "prev_sql": state.get("sql", ""),
        "sql": sql,
        "stop_reason": res.stop_reason,
        "llm_calls": state.get("llm_calls", []) + [res],
    }


async def _generate(state: State) -> State:
    out = await _ask(state, [{"role": "user", "content": state["ctx"]["user"]}])
    out["first_sql"] = out["sql"]
    out["trace"] = [{"step": "generate", "sql": out["sql"]}]
    return out


async def _execute(state: State) -> State:
    try:
        res = await asyncio.to_thread(
            run_readonly,
            state["ctx"]["db_path"],
            state["sql"],
            timeout_s=EXEC_TIMEOUT_S,
            max_rows=state["ctx"]["max_rows"],
            policy=rls.policy_for(state["ctx"]["db_id"], state["ctx"].get("role")),
        )
        return {
            "exec_error": "",
            "rows": res.rows,
            "columns": res.columns,
            "truncated": res.truncated,
        }
    except (SQLRejected, TimeoutError, sqlite3.Error) as e:
        return {"exec_error": str(e), "rows": None, "columns": None, "truncated": False}


def _make_check(value_check: bool):
    async def check(state: State) -> State:
        issues = []
        if state["exec_error"]:
            issues.append(f"error: {state['exec_error']}")
        elif state["rows"] == []:
            issues.append("empty result")
        if value_check and not state["exec_error"]:
            issues += await asyncio.to_thread(
                check_values, state["ctx"]["db_id"], state["sql"], state["ctx"].get("role")
            )
        preview = state["rows"][:PREVIEW_ROWS] if state["rows"] else state["rows"]
        trace = state["trace"] + [{"step": "check", "issues": issues, "rows": preview}]
        return {"issues": issues, "trace": trace}

    return check


async def _correct(state: State) -> State:
    messages = state["messages"] + [{"role": "user", "content": feedback_message(state)}]
    out = await _ask(state, messages)
    if not out["sql"]:
        # 纠错这次没产出 SQL（如 max_tokens）：退回上一版，不交白卷
        out["sql"] = state["sql"]
    out["corrections"] = state.get("corrections", 0) + 1
    out["trace"] = state["trace"] + [{"step": "correct", "sql": out["sql"]}]
    return out


def _after_generate(state: State) -> str:
    return "execute" if state["sql"] else END


def _after_correct(state: State) -> str:
    # 模型返回同一条 SQL（确认原答案，或纠错失败已退回上一版），不再循环
    return END if state["sql"] == state["prev_sql"] else "execute"


class FixedPipeline:
    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self.graph = self._build_graph()

    def _build_graph(self):
        max_corrections = self.cfg.max_corrections

        def after_check(state: State) -> str:
            if not state["issues"] or state.get("corrections", 0) >= max_corrections:
                return END
            return "correct"

        g = StateGraph(State)
        g.add_node("generate", _generate)
        g.add_node("execute", _execute)
        g.add_node("check", _make_check(self.cfg.value_check))
        g.add_node("correct", _correct)
        g.add_edge(START, "generate")
        g.add_conditional_edges("generate", _after_generate, ["execute", END])
        g.add_edge("execute", "check")
        g.add_conditional_edges("check", after_check, ["correct", END])
        g.add_conditional_edges("correct", _after_correct, ["execute", END])
        return g.compile()

    async def astream(
        self, llm: LLM, db_path: str | Path, question: str, evidence: str, role: str | None = None
    ):
        """逐节点产出 (节点名, 更新)，最后产出 ("result", GenerationResult)。供前端显示进度。"""
        db_path = Path(db_path)
        ctx: Ctx = {
            "llm": llm,
            "system": build_system(self.cfg, db_path, question, evidence, role),
            "db_path": db_path,
            "db_id": db_path.stem,
            "user": user_message(question, evidence),
            "max_rows": self.cfg.result_rows,
            "role": role,
        }
        state: dict = {"ctx": ctx, "corrections": 0, "llm_calls": [], "trace": []}
        async for chunk in self.graph.astream(state, stream_mode="updates"):
            for node, update in chunk.items():
                state.update(update or {})
                yield node, update or {}
        yield (
            "result",
            GenerationResult(
                sql=state.get("sql", ""),
                stop_reason=state.get("stop_reason", ""),
                llm_calls=state.get("llm_calls", []),
                first_sql=state.get("first_sql", ""),
                trace=state.get("trace", []),
                exec_error=state.get("exec_error", ""),
                columns=state.get("columns"),
                rows=state.get("rows"),
                truncated=state.get("truncated", False),
            ),
        )

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
        return len(build_system(self.cfg, Path(db_path), question, evidence, role)) + len(
            user_message(question, evidence)
        )
