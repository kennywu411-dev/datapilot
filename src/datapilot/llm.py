"""模型调用封装（复用自 OpsPilot）。Agent 只依赖 LLM 协议，测试时换成 ScriptedLLM，不花钱也能跑。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel

from datapilot.config import DEFAULT_MODEL, cost_usd


@dataclass
class LLMResult:
    content: list[dict]  # 原样保留的内容块（含 thinking 块，下一次请求要原样带回）
    stop_reason: str
    model: str
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    parsed: Any = None  # 结构化输出解析结果；refusal 或解析失败时为 None

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.model, self.usage)

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLM(Protocol):
    model: str

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_format: type[BaseModel] | None = None,
    ) -> LLMResult: ...


class ClaudeLLM:
    """Claude Messages API。

    - adaptive thinking + 显式 effort：三档模型在同一 effort 下对比才公平
    - 系统提示和工具定义固定，打 cache_control，多轮调用时走缓存
    - 不开启 refusal fallback：评测要比较的是指定模型本身，换模型作答会污染结果；
      refusal 记为一种失败类型
    - output_format 传 Pydantic 模型时走结构化输出，避免从自由文本里抠 SQL
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = 16000,
    ):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.AsyncAnthropic(max_retries=6)

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_format: type[BaseModel] | None = None,
    ) -> LLMResult:
        start = time.perf_counter()
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }
        if tools:
            kwargs["tools"] = tools
        parsed = None
        if output_format is None:
            resp = await self.client.messages.create(**kwargs)
        else:
            resp = await self.client.messages.parse(output_format=output_format, **kwargs)
            if resp.stop_reason not in ("refusal", "max_tokens"):
                parsed = resp.parsed_output
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
            "cache_read_input_tokens": u.cache_read_input_tokens or 0,
        }
        return LLMResult(
            content=[b.to_dict() for b in resp.content],
            stop_reason=resp.stop_reason or "end_turn",
            model=self.model,
            usage=usage,
            latency_s=time.perf_counter() - start,
            parsed=parsed,
        )


class ScriptedLLM:
    """按顺序返回预先写好的回复，用于单元测试。

    每一步可以是字符串（最终回答）、Pydantic 实例（结构化输出）、
    dict（单个工具调用 {"name":..., "input":...}）或 dict 列表（并行工具调用）。
    """

    def __init__(self, script: list, model: str = "scripted"):
        self.script = list(script)
        self.model = model
        self.calls: list[dict] = []

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_format: type[BaseModel] | None = None,
    ) -> LLMResult:
        self.calls.append({"system": system, "messages": [m.copy() for m in messages]})
        if not self.script:
            raise RuntimeError("ScriptedLLM: script exhausted")
        step = self.script.pop(0)
        if isinstance(step, BaseModel):
            text = step.model_dump_json()
            return LLMResult([{"type": "text", "text": text}], "end_turn", self.model, parsed=step)
        if isinstance(step, str):
            return LLMResult([{"type": "text", "text": step}], "end_turn", self.model)
        steps = step if isinstance(step, list) else [step]
        n = len(self.calls)
        content = [
            {
                "type": "tool_use",
                "id": f"tu_{n}_{i}",
                "name": s["name"],
                "input": s.get("input", {}),
            }
            for i, s in enumerate(steps)
        ]
        return LLMResult(content, "tool_use", self.model)
