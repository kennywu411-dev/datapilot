"""Agent 版本：模型自主调用 search_values / run_sql，用 submit_answer 交卷。"""

import asyncio
import json

from datapilot.agent import TOOLS, SQLAgent
from datapilot.config import db_path
from datapilot.llm import ScriptedLLM


def run(agent, llm, q="q", e=""):
    return asyncio.run(agent(llm, db_path("shop"), q, e))


def tool_results(call):
    msg = call["messages"][-1]
    return [json.loads(b["content"]) for b in msg["content"] if b["type"] == "tool_result"]


def test_tool_specs_are_strict():
    for t in TOOLS:
        assert t["strict"] is True
        schema = t["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_explore_then_submit(shop_db_dir):
    llm = ScriptedLLM(
        [
            {
                "name": "search_values",
                "input": {"keyword": "female", "table": None, "column": None},
            },
            {"name": "run_sql", "input": {"sql": "SELECT count(*) FROM users WHERE gender = 'F'"}},
            {
                "name": "submit_answer",
                "input": {"sql": "SELECT count(*) FROM users WHERE gender = 'F'"},
            },
        ]
    )
    out = run(SQLAgent(), llm)
    assert out.sql == "SELECT count(*) FROM users WHERE gender = 'F'"
    assert len(out.llm_calls) == 3
    # run_sql 的结果作为 tool_result 回传给模型
    assert tool_results(llm.calls[2])[0]["rows"] == [[2]]
    assert [t["tool"] for t in out.trace] == ["search_values", "run_sql", "submit_answer"]


def test_schema_in_cached_system_and_tool_errors_are_returned(shop_db_dir):
    llm = ScriptedLLM(
        [
            {"name": "run_sql", "input": {"sql": "DELETE FROM users"}},
            {"name": "submit_answer", "input": {"sql": "SELECT 1"}},
        ]
    )
    run(SQLAgent(), llm)
    assert "CREATE TABLE users" in llm.calls[0]["system"]
    assert tool_results(llm.calls[1])[0]["error"] == "rejected"


def test_no_submit_nudges_once_then_falls_back_to_last_run_sql(shop_db_dir):
    llm = ScriptedLLM(
        [
            {"name": "run_sql", "input": {"sql": "SELECT name FROM users"}},
            "我查到了结果。",
            "还是不调用工具。",
        ]
    )
    out = run(SQLAgent(), llm)
    assert "submit_answer" in llm.calls[2]["messages"][-1]["content"]
    assert out.sql == "SELECT name FROM users"


def test_step_limit(shop_db_dir):
    llm = ScriptedLLM([{"name": "run_sql", "input": {"sql": f"SELECT {i}"}} for i in range(20)])
    out = run(SQLAgent(max_steps=4), llm)
    assert len(out.llm_calls) == 4
    assert out.sql == "SELECT 3"  # 达到上限，取最后一次执行的 SQL


def test_parallel_tool_calls(shop_db_dir):
    llm = ScriptedLLM(
        [
            [
                {
                    "name": "search_values",
                    "input": {"keyword": "Boston", "table": None, "column": None},
                },
                {"name": "run_sql", "input": {"sql": "SELECT count(*) FROM users"}},
            ],
            {"name": "submit_answer", "input": {"sql": "SELECT count(*) FROM users"}},
        ]
    )
    out = run(SQLAgent(), llm)
    assert len(tool_results(llm.calls[1])) == 2
    assert out.sql == "SELECT count(*) FROM users"


def test_agent_first_sql_is_final_and_text_is_traced(shop_db_dir):
    """Agent 的探索查询不算"纠错前答案"；文字回复记入 trace 以便检查泄露。"""
    llm = ScriptedLLM(
        [
            {"name": "run_sql", "input": {"sql": "SELECT DISTINCT gender FROM users"}},
            "先说明一下思路。",
            {"name": "submit_answer", "input": {"sql": "SELECT count(*) FROM users"}},
        ]
    )
    out = run(SQLAgent(), llm)
    assert out.first_sql == out.sql == "SELECT count(*) FROM users"
    assert any(t["tool"] == "text" and "思路" in t["output"] for t in out.trace)
