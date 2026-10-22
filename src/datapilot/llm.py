"""模型调用封装（复用自 OpsPilot）。Agent 只依赖 LLM 协议，测试时换成 ScriptedLLM，不花钱也能跑。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ValidationError

from datapilot.config import DEFAULT_MODEL, cost_usd


@dataclass
class LLMResult:
    content: list[dict]  # 原样保留的内容块（含 thinking 块，下一次请求要原样带回）
    stop_reason: str
    model: str
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    parsed: Any = None  # 结构化输出解析结果；refusal 或解析失败时为 None
    retried: bool = False  # 撞到 max_tokens 后是否重试过（P8）

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
    - output_format 传 Pydantic 模型时走结构化输出，避免从自由文本里抠 SQL。
      用 create + 自己解析而不是 messages.parse：后者遇到截断的 JSON 会直接抛异常，
      这次调用的 usage 就丢了，花费记不上账
    - max_tokens 4096（正常调用单次输出最大 885 token）；撞到上限重试一次（P8：Sonnet
      偶发生成失控，每次白白耗尽 16000 token）
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = 4096,
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
        first = await self._once(system, messages, tools, output_format)
        if first.stop_reason != "max_tokens":
            first.latency_s = time.perf_counter() - start
            return first
        second = await self._once(system, messages, tools, output_format)
        second.usage = {k: first.usage.get(k, 0) + v for k, v in second.usage.items()}
        second.retried = True
        second.latency_s = time.perf_counter() - start
        return second

    async def _once(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_format: type[BaseModel] | None,
    ) -> LLMResult:
        output_config: dict = {"effort": self.effort}
        if output_format is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": anthropic.transform_schema(output_format),
            }
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
        }
        if tools:
            kwargs["tools"] = tools
        resp = await self.client.messages.create(**kwargs)
        content = [b.to_dict() for b in resp.content]
        parsed = None
        if output_format is not None and resp.stop_reason not in ("refusal", "max_tokens"):
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            try:
                parsed = output_format.model_validate_json(text)
            except ValidationError:
                parsed = None
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
            "cache_read_input_tokens": u.cache_read_input_tokens or 0,
        }
        return LLMResult(
            content=content,
            stop_reason=resp.stop_reason or "end_turn",
            model=self.model,
            usage=usage,
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
