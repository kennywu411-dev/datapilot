"""固定流程基线：全量 schema + evidence，一次生成 SQL。

提示词照搬 BIRD 官方 llm/src/prompt.py（schema DDL + 问题 + External Knowledge + CoT），
唯一改动：官方要求模型"只返回 SQL、不要 ``` 包裹"再从文本里取；这里改用结构化输出，
SQL 放在 sql 字段，避免格式问题干扰准确率。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from datapilot.llm import LLM, LLMResult
from datapilot.schema import schema_ddl

SYSTEM = "You are an expert in SQLite text-to-SQL."

FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE)


class SQLAnswer(BaseModel):
    sql: str = Field(description="One valid SQLite SELECT statement answering the question.")


@dataclass
class GenerationResult:
    sql: str
    stop_reason: str
    llm_calls: list[LLMResult] = field(default_factory=list)
    first_sql: str = ""  # 纠错前的第一版 SQL，用于统计"改对 / 改错"
    trace: list[dict] = field(default_factory=list)
    # 流程最后一次执行的结果（服务层直接复用，不再执行第二遍）
    exec_error: str = ""
    columns: list[str] | None = None
    rows: list | None = None
    truncated: bool = False


def build_prompt(db_path: str | Path, question: str, evidence: str) -> str:
    knowledge = evidence.strip()
    comment = (
        f"-- Using valid SQLite{' and understanding External Knowledge' if knowledge else ''}, "
        "answer the following questions for the tables provided above.\n"
        f"-- {question}\n"
        f"{f'-- External Knowledge: {knowledge}' if knowledge else ''}"
    )
    cot = "Generate the SQLite for the above question after thinking step by step."
    instruction = "Return only the final SQLite SQL query in the `sql` field, starting from SELECT."
    return "\n\n".join([schema_ddl(str(db_path)), comment, cot, instruction])


def clean_sql(sql: str) -> str:
    return FENCE_RE.sub("", sql.strip()).strip()


async def generate_sql(
    llm: LLM, db_path: str | Path, question: str, evidence: str
) -> GenerationResult:
    prompt = build_prompt(db_path, question, evidence)
    res = await llm.complete(
        SYSTEM, [{"role": "user", "content": prompt}], [], output_format=SQLAnswer
    )
    sql = clean_sql(res.parsed.sql) if res.parsed else ""
    return GenerationResult(sql=sql, stop_reason=res.stop_reason, llm_calls=[res], first_sql=sql)
