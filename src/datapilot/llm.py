"""模型调用封装（复用自 OpsPilot）。Agent 只依赖 LLM 协议，测试时换成 ScriptedLLM，不花钱也能跑。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Protocol

import anthropic
import httpx2
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


@cache
def _json_schema(output_format: type[BaseModel]) -> dict:
    return anthropic.transform_schema(output_format)


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
                "schema": _json_schema(output_format),
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


class LocalLLM:
    """本地开源模型（Ollama /api/chat）。回答"国内用不了 Claude 怎么办"。

    - 只用于固定流程（不传 tools）：7B 模型的工具调用不稳定，第 6 节已预判
    - format 传 JSON schema 约束输出，SQL 放在 sql 字段，与 Claude 走同一套解析
    - num_ctx 8192：最大的增强 schema 约 5700 token，默认 2048 会被截断
    - temperature 0：尽量可复现；花费记为 0（本地电费不计），延迟照实记录
    """

    def __init__(
        self,
        model: str = "qwen2.5-coder:7b",
        client: httpx2.AsyncClient | None = None,
        base_url: str = "http://localhost:11434",
        num_ctx: int = 8192,
        max_tokens: int = 1024,
    ):
        self.model = model
        self.base_url = base_url
        self.num_ctx = num_ctx
        self.max_tokens = max_tokens
        self.client = client or httpx2.AsyncClient(timeout=600)

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, str):
            return content
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_format: type[BaseModel] | None = None,
    ) -> LLMResult:
        if tools:
            raise ValueError("LocalLLM 只用于固定流程，不支持工具调用")
        start = time.perf_counter()
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m["role"], "content": self._text(m["content"])} for m in messages],
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": self.max_tokens},
        }
        if output_format is not None:
            body["format"] = output_format.model_json_schema()
        resp = await self.client.post(f"{self.base_url}/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("message", {}).get("content", "")
        stop = "max_tokens" if data.get("done_reason") == "length" else "end_turn"
        parsed = None
        if output_format is not None and stop != "max_tokens":
            try:
                parsed = output_format.model_validate_json(text)
            except ValidationError:
                parsed = None
        return LLMResult(
            content=[{"type": "text", "text": text}],
            stop_reason=stop,
            model=self.model,
            usage={
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
            },
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
