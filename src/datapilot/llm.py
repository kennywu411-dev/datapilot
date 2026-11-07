"""模型调用封装。Agent 只依赖 LLM 协议，测试时换成 ScriptedLLM，不花钱也能跑。"""

from __future__ import annotations

import asyncio
import json
import re
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


async def retry_on_max_tokens(once) -> LLMResult:
    """撞到 max_tokens 时重试一次，usage 两次累计（P8）。ClaudeLLM 与 LocalLLM 共用。"""
    start = time.perf_counter()
    first = await once()
    if first.stop_reason != "max_tokens":
        first.latency_s = time.perf_counter() - start
        return first
    second = await once()
    second.usage = {k: first.usage.get(k, 0) + v for k, v in second.usage.items()}
    second.retried = True
    second.latency_s = time.perf_counter() - start
    return second


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
        return await retry_on_max_tokens(lambda: self._once(system, messages, tools, output_format))

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
        chat = [{"role": "system", "content": system}] + [
            {"role": m["role"], "content": self._text(m["content"])} for m in messages
        ]
        # 超过 num_ctx 时 Ollama 只保留开头 4 个 token、静默丢掉之后的开头部分，schema 正好在开头。
        # 按 3 字符/token 保守估算，宁可报错也不让模型在看不到 schema 的情况下作答（代码审查发现）。
        est = sum(len(m["content"]) for m in chat) / 3
        if est + self.max_tokens > self.num_ctx:
            raise ValueError(f"提示约 {est:.0f} token，加输出上限超过 num_ctx={self.num_ctx}")
        return await retry_on_max_tokens(lambda: self._once(chat, output_format))

    async def _once(self, chat: list[dict], output_format: type[BaseModel] | None) -> LLMResult:
        body: dict = {
            "model": self.model,
            "messages": chat,
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
            parsed=parsed,
        )


RETRY_BASE_S = 1.0
RETRY_STATUS = {429, 500, 502, 503, 504}
FATAL_STATUS = {401, 402, 403}  # 密钥无效、欠费、额度用完：重试没用，整个评测应该停下


class FatalAPIError(RuntimeError):
    """账户层面的错误（不是这一题的问题）。评测遇到它就停，避免把剩下的题全记成失败。"""


FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_loose(text: str, output_format: type[BaseModel]) -> BaseModel | None:
    """先按严格 JSON 解析；失败时去掉代码块包装、截取第一个 {...}、字段名不区分大小写再试。

    国产模型在 JSON 模式下偶尔把 JSON 包进 ```json 代码块，或把 sql 写成 SQL（v1.3 实测）。
    """
    try:
        return output_format.model_validate_json(text)
    except ValidationError:
        pass
    body = FENCE.sub("", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    lower = {str(k).lower(): v for k, v in obj.items()}
    fields = {
        name: lower[name.lower()] for name in output_format.model_fields if name.lower() in lower
    }
    try:
        return output_format.model_validate(fields)
    except ValidationError:
        return None


class OpenAICompatLLM:
    """OpenAI 兼容接口（DeepSeek、阿里云百炼的通义千问）。回答"国内用什么模型"。

    - 与 LocalLLM 一样只用于固定流程（不传 tools）
    - 结构化输出用 JSON 模式（json_object）：两家都支持，json_schema 支持情况不一；
      JSON 模式要求提示里出现 "json"，所以在 system 末尾给字段示例（不附完整 schema，P17）
    - temperature 0；extra_body 透传厂商参数（百炼用 enable_thinking=False 关掉思考，
      与 Claude 对比时统一说明）
    - usage 拆出缓存命中部分单独计价：DeepSeek 返回 prompt_cache_hit_tokens，
      百炼返回 prompt_tokens_details.cached_tokens
    - 429 / 5xx 和网络错误指数退避重试（Claude SDK 自带 max_retries，这里没有 SDK，自己做）
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        client: httpx2.AsyncClient | None = None,
        max_tokens: int = 4096,
        extra_body: dict | None = None,
        max_retries: int = 6,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self.max_retries = max_retries
        self.client = client or httpx2.AsyncClient(timeout=300)

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_format: type[BaseModel] | None = None,
    ) -> LLMResult:
        if tools:
            raise ValueError("OpenAICompatLLM 只用于固定流程，不支持工具调用")
        if output_format is not None:
            # 只给字段示例，不附完整 schema：qwen3.7-plus 约 1/5 的题会把 schema 原样抄回（P17）
            example = json.dumps({k: "..." for k in output_format.model_fields})
            system = (
                f"{system}\n\nReply with only one JSON object of the form {example}, "
                "filling in your answer. Do not output anything else."
            )
        chat = [{"role": "system", "content": system}] + [
            {"role": m["role"], "content": LocalLLM._text(m["content"])} for m in messages
        ]
        return await retry_on_max_tokens(lambda: self._once(chat, output_format))

    async def _post(self, body: dict) -> dict:
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx2.TransportError:  # 连接失败、读超时：和 Claude SDK 一样重试
                if attempt == self.max_retries:
                    raise
                await asyncio.sleep(RETRY_BASE_S * 2**attempt)
                continue
            if resp.status_code in FATAL_STATUS:
                raise FatalAPIError(f"HTTP {resp.status_code}：{resp.text[:300]}")
            if resp.status_code not in RETRY_STATUS or attempt == self.max_retries:
                resp.raise_for_status()
                return resp.json()
            await asyncio.sleep(RETRY_BASE_S * 2**attempt)
        raise AssertionError("unreachable")

    async def _once(self, chat: list[dict], output_format: type[BaseModel] | None) -> LLMResult:
        body: dict = {
            "model": self.model,
            "messages": chat,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            **self.extra_body,
        }
        if output_format is not None:
            body["response_format"] = {"type": "json_object"}
        data = await self._post(body)
        choice = data["choices"][0]
        text = choice.get("message", {}).get("content") or ""
        stop = "max_tokens" if choice.get("finish_reason") == "length" else "end_turn"
        parsed = None
        if output_format is not None and stop != "max_tokens":
            parsed = parse_loose(text, output_format)
        u = data.get("usage") or {}
        hit = u.get("prompt_cache_hit_tokens")
        if hit is None:
            hit = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return LLMResult(
            content=[{"type": "text", "text": text}],
            stop_reason=stop,
            model=self.model,
            usage={
                "input_tokens": u.get("prompt_tokens", 0) - hit,
                "output_tokens": u.get("completion_tokens", 0),
                "cache_read_input_tokens": hit,
            },
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


class SimulatedLLM:
    """压测用：只睡一段时间模拟模型延迟，返回固定 SQL 和回答，不调用任何 API、花费为 0。

    用来测服务端自身（事件循环、存储、并发保护）的表现，不代表真实模型的准确率或延迟分布。
    """

    model = "simulated"

    def __init__(self, latency_s: float = 2.0):
        self.latency_s = latency_s

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_format: type[BaseModel] | None = None,
    ) -> LLMResult:
        await asyncio.sleep(self.latency_s)
        if output_format is not None:
            parsed = output_format.model_validate({"sql": "SELECT 1 AS n"})
            text = parsed.model_dump_json()
            return LLMResult(
                [{"type": "text", "text": text}], "end_turn", self.model, parsed=parsed
            )
        return LLMResult([{"type": "text", "text": "结果是 1。"}], "end_turn", self.model)
