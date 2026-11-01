"""智能路由：便宜模型先答，出现失败信号才升级到强模型重做（v1.3，ADR 004）。

为什么不按难度分：BIRD 的 simple / moderate / challenging 是人工标注，真实用户提问时没有，
只能用运行时看得到的信号。三条规则事先定好，离线回放和实跑都报全部结果，不在同一批题上挑规则。

- r1_any_fix：触发过纠错（第一版 SQL 报错、空结果或取值不存在）就升级
- r2_unresolved：纠错用完仍有问题、或没产出 SQL 才升级（更省，但会漏掉"改了但仍错"）
- r3_fix_or_complex：r1，或者便宜模型的 SQL 关联了 3 张及以上的表（复杂题更容易错）
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from datapilot.baseline import GenerationResult
from datapilot.llm import LLM

JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)


def _last_issues(trace: list[dict]) -> list:
    checks = [t for t in trace if t.get("step") == "check"]
    return checks[-1].get("issues", []) if checks else []


def any_fix(sql: str, n_calls: int, trace: list[dict]) -> bool:
    return not sql or n_calls > 1 or bool(_last_issues(trace))


def unresolved(sql: str, n_calls: int, trace: list[dict]) -> bool:
    return not sql or bool(_last_issues(trace))


def fix_or_complex(sql: str, n_calls: int, trace: list[dict]) -> bool:
    return any_fix(sql, n_calls, trace) or len(JOIN_RE.findall(sql)) >= 2


# 规则只看 (SQL, 调用次数, 轨迹)：评测记录里都有，离线回放和实跑用同一个函数
RULES: dict[str, Callable[[str, int, list[dict]], bool]] = {
    "r1_any_fix": any_fix,
    "r2_unresolved": unresolved,
    "r3_fix_or_complex": fix_or_complex,
}


@dataclass
class LLMPair:
    """路由用的一对模型。model 字段让它能放进只认单个 LLM 的评测记录里。"""

    cheap: LLM
    strong: LLM
    model: str = "route"


class RoutedPipeline:
    """同一条固定流程先用便宜模型跑；规则判定要升级时，用强模型从头再跑一遍。

    强模型从头跑而不是接着便宜模型的结果改：两次互不影响，离线回放才能用已有的独立运行模拟。
    两次的调用都记进 llm_calls（各按自己的模型计价）；轨迹里插入 escalate 步骤，便于统计升级率。
    """

    def __init__(self, base, rule: str):
        self.base = base
        self.rule = rule
        self.escalate = RULES[rule]

    async def __call__(
        self,
        llm: LLMPair,
        db_path: str | Path,
        question: str,
        evidence: str,
        role: str | None = None,
    ) -> GenerationResult:
        first = await self.base(llm.cheap, db_path, question, evidence, role)
        if not self.escalate(first.sql, len(first.llm_calls), first.trace):
            return first
        second = await self.base(llm.strong, db_path, question, evidence, role)
        second.llm_calls = first.llm_calls + second.llm_calls
        second.trace = first.trace + [{"step": "escalate", "rule": self.rule}] + second.trace
        # first_sql 保持强模型自己的第一版：runner 用它统计"纠错改对 / 改错"，
        # 换成便宜模型的第一版会把"换模型"误算成纠错效果
        return second

    def input_chars(
        self, db_path: str | Path, question: str, evidence: str, role: str | None = None
    ) -> int:
        return self.base.input_chars(db_path, question, evidence, role)
