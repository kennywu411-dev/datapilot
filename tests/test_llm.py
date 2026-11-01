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


def test_local_llm_request_and_parse():
    """LocalLLM（Ollama）：JSON schema 约束输出，content 块转成纯文本，花费为 0。"""
    from datapilot.llm import LocalLLM

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": '{"sql": "SELECT 7"}'},
                "done_reason": "stop",
                "prompt_eval_count": 1200,
                "eval_count": 30,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = LocalLLM("qwen2.5-coder:7b", client=client)
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "text", "text": '{"sql": "SELECT 1"}'}]},
        {"role": "user", "content": "fix it"},
    ]
    res = asyncio.run(llm.complete("系统", history, [], output_format=SQLAnswer))
    body = seen["body"]
    assert body["messages"][0] == {"role": "system", "content": "系统"}
    assert body["messages"][2] == {"role": "assistant", "content": '{"sql": "SELECT 1"}'}
    assert body["format"]["properties"]["sql"]["type"] == "string"
    assert body["options"]["temperature"] == 0 and body["options"]["num_ctx"] >= 8192
    assert body["stream"] is False
    assert res.parsed == SQLAnswer(sql="SELECT 7")
    assert res.usage == {"input_tokens": 1200, "output_tokens": 30}
    assert res.cost_usd == 0.0


def test_local_llm_truncated_output_is_max_tokens():
    from datapilot.llm import LocalLLM

    def handler(request):
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": '{"sql'}, "done_reason": "length"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = asyncio.run(
        LocalLLM("m", client=client).complete(
            "s", [{"role": "user", "content": "q"}], [], SQLAnswer
        )
    )
    assert res.stop_reason == "max_tokens" and res.parsed is None


def test_local_llm_rejects_prompt_over_context():
    """提示超过 num_ctx 时 Ollama 会静默截掉开头（schema 就在开头），必须提前报错。"""
    import pytest

    from datapilot.llm import LocalLLM

    called = []

    def handler(request):
        called.append(1)
        return httpx.Response(200, json={})

    llm = LocalLLM(
        "m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), num_ctx=100
    )
    with pytest.raises(ValueError, match="num_ctx"):
        asyncio.run(llm.complete("x" * 1000, [{"role": "user", "content": "q"}], [], SQLAnswer))
    assert called == []


def test_local_llm_retries_once_on_length():
    from datapilot.llm import LocalLLM

    seen = []

    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={"message": {"content": '{"sql'}, "done_reason": "length", "eval_count": 9},
            )
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"sql": "SELECT 1"}'},
                "done_reason": "stop",
                "eval_count": 5,
            },
        )

    llm = LocalLLM("m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    res = asyncio.run(llm.complete("s", [{"role": "user", "content": "q"}], [], SQLAnswer))
    assert len(seen) == 2 and res.retried and res.parsed == SQLAnswer(sql="SELECT 1")
    assert res.usage["output_tokens"] == 14


async def test_simulated_llm_sleeps_and_returns_structured_sql():
    import time

    from datapilot.baseline import SQLAnswer
    from datapilot.llm import SimulatedLLM

    llm = SimulatedLLM(latency_s=0.1)
    start = time.perf_counter()
    res = await llm.complete("sys", [{"role": "user", "content": "q"}], [], output_format=SQLAnswer)
    assert time.perf_counter() - start >= 0.1
    assert res.parsed.sql == "SELECT 1 AS n" and res.cost_usd == 0
    text = await llm.complete("sys", [{"role": "user", "content": "q"}], [])
    assert text.text and text.parsed is None


def _compat(handler, **kw):
    from datapilot.llm import OpenAICompatLLM

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatLLM(
        "deepseek-test", base_url="https://api.example.com/v1", api_key="k", client=client, **kw
    )


def _chat_resp(content, finish="stop", usage=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
            ],
            "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 50},
        },
    )


def test_openai_compat_request_and_parse():
    """OpenAI 兼容接口（DeepSeek / 通义千问）：JSON 模式、system 带 schema、内容转纯文本。"""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return _chat_resp('{"sql": "SELECT 7"}')

    llm = _compat(handler, extra_body={"enable_thinking": False})
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "text", "text": '{"sql": "SELECT 1"}'}]},
        {"role": "user", "content": "fix it"},
    ]
    res = asyncio.run(llm.complete("系统", history, [], output_format=SQLAnswer))
    body = seen["body"]
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert body["model"] == "deepseek-test" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["enable_thinking"] is False
    sys_msg = body["messages"][0]
    assert sys_msg["role"] == "system" and sys_msg["content"].startswith("系统")
    assert "JSON" in sys_msg["content"] and '"sql"' in sys_msg["content"]
    assert body["messages"][2] == {"role": "assistant", "content": '{"sql": "SELECT 1"}'}
    assert res.parsed == SQLAnswer(sql="SELECT 7")
    assert res.usage == {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 0}


def test_openai_compat_cache_usage_both_vendors():
    """缓存命中单独计价：DeepSeek 用 prompt_cache_hit_tokens，百炼用 prompt_tokens_details。"""
    ds = {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_cache_hit_tokens": 800}
    qw = {
        "prompt_tokens": 1000,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 600},
    }
    for usage, hit in ((ds, 800), (qw, 600)):
        llm = _compat(lambda r, u=usage: _chat_resp('{"sql": "SELECT 1"}', usage=u))
        res = asyncio.run(llm.complete("s", [{"role": "user", "content": "q"}], [], SQLAnswer))
        assert res.usage == {
            "input_tokens": 1000 - hit,
            "output_tokens": 10,
            "cache_read_input_tokens": hit,
        }


def test_openai_compat_length_retries_once():
    seen = []

    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            return _chat_resp('{"sql', finish="length")
        return _chat_resp('{"sql": "SELECT 1"}')

    res = asyncio.run(
        _compat(handler).complete("s", [{"role": "user", "content": "q"}], [], SQLAnswer)
    )
    assert len(seen) == 2 and res.retried and res.parsed == SQLAnswer(sql="SELECT 1")


def test_openai_compat_bad_json_is_unparsed_not_crash():
    res = asyncio.run(
        _compat(lambda r: _chat_resp("SELECT 1")).complete(
            "s", [{"role": "user", "content": "q"}], [], SQLAnswer
        )
    )
    assert res.parsed is None and res.stop_reason == "end_turn"


def test_openai_compat_retries_rate_limit(monkeypatch):
    """429 / 5xx 退避重试（Claude SDK 自带 max_retries，这里自己实现）。"""
    import datapilot.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RETRY_BASE_S", 0)
    seen = []

    def handler(request):
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(429 if len(seen) == 1 else 503, json={})
        return _chat_resp('{"sql": "SELECT 1"}')

    res = asyncio.run(
        _compat(handler).complete("s", [{"role": "user", "content": "q"}], [], SQLAnswer)
    )
    assert len(seen) == 3 and res.parsed == SQLAnswer(sql="SELECT 1")


def test_openai_compat_rejects_tools():
    import pytest

    with pytest.raises(ValueError, match="工具"):
        asyncio.run(_compat(lambda r: _chat_resp("")).complete("s", [], [{"name": "t"}]))


def test_parse_loose_handles_fences_and_key_case():
    from datapilot.llm import parse_loose

    assert parse_loose('{"sql": "SELECT 1"}', SQLAnswer) == SQLAnswer(sql="SELECT 1")
    assert parse_loose('```json\n{"sql": "SELECT 2"}\n```', SQLAnswer) == SQLAnswer(sql="SELECT 2")
    assert parse_loose('{"SQL": "SELECT 3"}', SQLAnswer) == SQLAnswer(sql="SELECT 3")
    assert parse_loose('好的：{"Sql": "SELECT 4"} 以上', SQLAnswer) == SQLAnswer(sql="SELECT 4")
    assert parse_loose("SELECT 5", SQLAnswer) is None
    assert parse_loose('{"query": "SELECT 6"}', SQLAnswer) is None


def test_openai_compat_fatal_status_not_retried():
    """403（如百炼"仅免费额度"用完）重试没用：直接抛 FatalAPIError，让评测停下。"""
    import pytest

    from datapilot.llm import FatalAPIError

    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(403, json={"error": {"code": "AllocationQuota.FreeTierOnly"}})

    with pytest.raises(FatalAPIError, match="403"):
        asyncio.run(
            _compat(handler).complete("s", [{"role": "user", "content": "q"}], [], SQLAnswer)
        )
    assert len(seen) == 1


def test_openai_compat_retries_transport_error(monkeypatch):
    """连接失败 / 读超时重试，不直接记成这一题失败。"""
    import datapilot.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RETRY_BASE_S", 0)
    seen = []

    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            raise httpx.ConnectError("boom", request=request)
        return _chat_resp('{"sql": "SELECT 1"}')

    res = asyncio.run(
        _compat(handler).complete("s", [{"role": "user", "content": "q"}], [], SQLAnswer)
    )
    assert len(seen) == 2 and res.parsed == SQLAnswer(sql="SELECT 1")
