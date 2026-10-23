"""智能路由：规则只看运行时信号；升级时强模型从头重跑，两边的调用都记账。"""

import asyncio

from datapilot.baseline import SQLAnswer
from datapilot.config import db_path
from datapilot.llm import ScriptedLLM
from datapilot.pipeline import FixedPipeline, PipelineConfig
from datapilot.routing import LLMPair, RoutedPipeline, any_fix, fix_or_complex, unresolved

E4 = FixedPipeline(PipelineConfig(schema="enhanced", value_check=True, max_corrections=3))
OK_TRACE = [{"step": "generate"}, {"step": "check", "issues": []}]
BAD_TRACE = [{"step": "generate"}, {"step": "check", "issues": ["empty result"]}]


def test_rules_on_runtime_signals():
    # 一次答对、没有问题：三条规则都不升级
    assert not any_fix("SELECT 1", 1, OK_TRACE)
    assert not unresolved("SELECT 1", 1, OK_TRACE)
    # 纠错后已解决：r1 升级，r2 不升级
    fixed = OK_TRACE + [{"step": "correct"}, {"step": "check", "issues": []}]
    assert any_fix("SELECT 1", 2, fixed) and not unresolved("SELECT 1", 2, fixed)
    # 纠错用完仍有问题、或没产出 SQL：都升级
    assert unresolved("SELECT 1", 4, BAD_TRACE) and any_fix("", 1, OK_TRACE)
    # r3：一次答对但关联了 3 张表，也升级
    sql3 = "SELECT 1 FROM a JOIN b ON 1 JOIN c ON 1"
    assert fix_or_complex(sql3, 1, OK_TRACE) and not any_fix(sql3, 1, OK_TRACE)


def test_no_escalation_uses_only_cheap(shop_db_dir):
    cheap = ScriptedLLM([SQLAnswer(sql="SELECT count(*) FROM users")], model="claude-haiku-5-5")
    strong = ScriptedLLM([], model="claude-sonnet-5-5")
    out = asyncio.run(
        RoutedPipeline(E4, "r1_any_fix")(LLMPair(cheap, strong), db_path("shop"), "q", "")
    )
    assert out.sql == "SELECT count(*) FROM users" and strong.calls == []
    assert not any(t["step"] == "escalate" for t in out.trace)


def test_escalation_reruns_with_strong_and_bills_both(shop_db_dir):
    bad = SQLAnswer(sql="SELECT nope FROM users")
    cheap = ScriptedLLM([bad] * 4, model="claude-haiku-5-5")  # 一直报错，纠错用完
    strong = ScriptedLLM([SQLAnswer(sql="SELECT id FROM users")], model="claude-sonnet-5-5")
    out = asyncio.run(
        RoutedPipeline(E4, "r2_unresolved")(LLMPair(cheap, strong), db_path("shop"), "q", "")
    )
    assert out.sql == "SELECT id FROM users"
    models = [c.model for c in out.llm_calls]
    assert "claude-haiku-5-5" in models and models[-1] == "claude-sonnet-5-5"
    assert any(t["step"] == "escalate" for t in out.trace)
    # first_sql 是强模型自己的第一版，纠错统计不把"换模型"算成纠错
    assert out.first_sql == "SELECT id FROM users"
    # 强模型从头开始：它的第一条消息里没有便宜模型的历史
    assert (
        strong.calls[0]["messages"][0]["role"] == "user" and len(strong.calls[0]["messages"]) == 1
    )


def test_runner_validates_route_pairing():
    import pytest

    from evals.runner import validate

    validate(["route"], "routed_r1")
    with pytest.raises(SystemExit, match="路由"):
        validate(["haiku"], "routed_r1")
    with pytest.raises(SystemExit, match="路由"):
        validate(["route"], "e4_correct")
