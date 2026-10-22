"""用假的 HTTP 传输层验证 ClaudeLLM 发出的请求和对响应的解析，不需要 API key。"""

import asyncio
import json

import anthropic
import httpx2 as httpx
from pydantic import BaseModel

from datapilot.llm import ClaudeLLM, ScriptedLLM

USAGE = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "cache_read_input_tokens": 2000,
    "cache_creation_input_tokens": 0,
}


def _client(handler):
    transport = httpx.MockTransport(handler)
    return anthropic.AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=transport)
    )


def _response(content, stop_reason="end_turn"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": USAGE,
    }


class SQLAnswer(BaseModel):
    sql: str


def test_complete_request_and_parse():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_response(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig123"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "run_sql",
                        "input": {"sql": "SELECT 1"},
                    },
                ],
                "tool_use",
            ),
        )

    llm = ClaudeLLM("claude-haiku-5-5", effort="low", client=_client(handler))
    tools = [{"name": "run_sql", "description": "x", "input_schema": {"type": "object"}}]
    res = asyncio.run(llm.complete("系统提示", [{"role": "user", "content": "q"}], tools))

    body = seen["body"]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in body  # 新模型不支持强制工具调用
    assert res.content[0] == {"type": "thinking", "thinking": "", "signature": "sig123"}
    assert res.tool_uses[0]["input"] == {"sql": "SELECT 1"}
    assert abs(res.cost_usd - (1000 * 0.10 + 2000 * 0.01 + 100 * 0.50) / 1e6) < 1e-12


def test_structured_output():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json=_response([{"type": "text", "text": '{"sql": "SELECT 2"}'}])
        )

    llm = ClaudeLLM("claude-haiku-5-5", client=_client(handler))
    res = asyncio.run(
        llm.complete("s", [{"role": "user", "content": "q"}], [], output_format=SQLAnswer)
    )
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    assert seen["body"]["output_config"]["effort"] == "medium"
    assert res.parsed == SQLAnswer(sql="SELECT 2")


def test_refusal_is_reported_not_raised():
    def handler(request):
        return httpx.Response(200, json=_response([], "refusal"))

    llm = ClaudeLLM("claude-haiku-5-5", client=_client(handler))
    res = asyncio.run(
        llm.complete("s", [{"role": "user", "content": "q"}], [], output_format=SQLAnswer)
    )
    assert res.stop_reason == "refusal" and res.parsed is None


def test_max_tokens_retries_once_and_sums_usage():
    """Sonnet 偶发生成失控（P8）：撞到 max_tokens 时重试一次，花费按两次累计。"""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(
                200, json=_response([{"type": "text", "text": '{"sql'}], "max_tokens")
            )
        return httpx.Response(
            200, json=_response([{"type": "text", "text": '{"sql": "SELECT 5"}'}])
        )

    llm = ClaudeLLM("claude-haiku-5-5", client=_client(handler))
    res = asyncio.run(
        llm.complete("s", [{"role": "user", "content": "q"}], [], output_format=SQLAnswer)
    )
    assert len(seen) == 2 and seen[0]["max_tokens"] == 4096
    assert res.parsed == SQLAnswer(sql="SELECT 5")
    assert res.usage["output_tokens"] == 200  # 两次各 100
    assert res.retried is True


def test_scripted_llm_structured():
    llm = ScriptedLLM([SQLAnswer(sql="SELECT 3"), "完成"])
    r1 = asyncio.run(llm.complete("s", [], [], output_format=SQLAnswer))
    r2 = asyncio.run(llm.complete("s", [], []))
    assert r1.parsed.sql == "SELECT 3"
    assert r2.text == "完成"
