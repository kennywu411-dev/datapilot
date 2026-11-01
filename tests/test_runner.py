import asyncio
import json
import sqlite3

import pytest

from datapilot.baseline import SQLAnswer
from datapilot.llm import ScriptedLLM
from evals.report import summarize, wilson
from evals.runner import Config, load_done, run, stratified_sample


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db_dir = tmp_path / "dbs"
    (db_dir / "toy").mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "toy" / "toy.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
    conn.commit()
    conn.close()
    monkeypatch.setattr("datapilot.config.DB_DIR", db_dir)
    questions = [
        {
            "question_id": i,
            "db_id": "toy",
            "question": f"q{i}",
            "evidence": "",
            "SQL": f"SELECT count(*) FROM t WHERE id < {i}",
            "difficulty": "simple",
        }
        for i in range(1, 5)
    ]
    return questions, tmp_path / "run"


def scripted_factory(answers: dict[int, str], calls: list):
    def factory(cfg, q):
        calls.append(q["question_id"])
        return ScriptedLLM([SQLAnswer(sql=answers[q["question_id"]])], model="claude-haiku-5-5")

    return factory


def test_run_scores_and_writes_jsonl(setup):
    questions, run_dir = setup
    answers = {
        1: "SELECT count(*) FROM t WHERE id < 1",
        2: "SELECT 0",
        3: "DROP TABLE t",
        4: "SELECT count(*) FROM t WHERE id <= 3",
    }
    calls = []
    cfg = Config(model="haiku", pipeline="baseline", effort="medium", repeat=0)
    rows = asyncio.run(run(questions, [cfg], scripted_factory(answers, calls), run_dir, 2))
    by_id = {r["question_id"]: r for r in rows}
    assert by_id[1]["correct"] and by_id[4]["correct"]
    assert not by_id[2]["correct"]
    assert by_id[3]["status"] == "pred_rejected"
    lines = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[0])["model"] == "haiku"


def test_resume_skips_done(setup):
    questions, run_dir = setup
    answers = {i: "SELECT 1" for i in range(1, 5)}
    cfg = Config(model="haiku", pipeline="baseline", effort="medium", repeat=0)
    first = []
    asyncio.run(run(questions[:2], [cfg], scripted_factory(answers, first), run_dir, 2))
    assert len(load_done(run_dir)) == 2
    second = []
    asyncio.run(run(questions, [cfg], scripted_factory(answers, second), run_dir, 2))
    assert sorted(second) == [3, 4]  # 前两题不再调用模型
    assert len((run_dir / "results.jsonl").read_text().splitlines()) == 4


def test_llm_exception_is_recorded_not_crash(setup):
    questions, run_dir = setup

    def factory(cfg, q):
        return ScriptedLLM([])  # 空脚本，调用即抛异常

    cfg = Config(model="haiku", pipeline="baseline", effort="medium", repeat=0)
    rows = asyncio.run(run(questions[:1], [cfg], factory, run_dir, 1))
    assert rows[0]["correct"] is False and rows[0]["status"] == "llm_error"
    # 出错的题不算完成，续跑时会重试
    assert load_done(run_dir) == set()


def test_max_cost_stops_new_calls(setup):
    questions, run_dir = setup
    from datapilot.llm import LLMResult

    class PricedLLM:
        model = "claude-opus-5-5"

        async def complete(self, system, messages, tools, output_format=None):
            # 每次 1M 输出 token = $20
            return LLMResult(
                [],
                "end_turn",
                self.model,
                usage={"output_tokens": 1_000_000},
                parsed=SQLAnswer(sql="SELECT 1"),
            )

    cfg = Config(model="opus", pipeline="baseline", effort="medium", repeat=0)
    rows = asyncio.run(run(questions, [cfg], lambda c, q: PricedLLM(), run_dir, 1, max_cost=30.0))
    assert len(rows) == 2  # 第 2 题后累计 $40 > $30，后两题不再调用
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(40.0)


def test_eval_exception_is_recorded_not_crash(setup, monkeypatch):
    """评测阶段的意外异常要记成一条失败，不能让 gather 中断整次运行。"""
    questions, run_dir = setup

    def boom(*a, **k):
        raise RuntimeError("评测器意外错误")

    monkeypatch.setattr("evals.runner.evaluate_one", boom)
    answers = {i: "SELECT 1" for i in range(1, 5)}
    cfg = Config(model="haiku", pipeline="baseline", effort="medium", repeat=0)
    rows = asyncio.run(run(questions, [cfg], scripted_factory(answers, []), run_dir, 2))
    assert len(rows) == 4 and all(r["status"] == "eval_error" for r in rows)


@pytest.mark.parametrize("n", [7, 20, 25, 33])
def test_stratified_sample_size_is_exact(n):
    qs = [
        {"question_id": i, "difficulty": d}
        for i, d in enumerate(["simple"] * 148 + ["moderate"] * 250 + ["challenging"] * 102)
    ]
    assert len(stratified_sample(qs, n)) == n


def test_stratified_sample_is_deterministic():
    qs = [
        {"question_id": i, "difficulty": d}
        for i, d in enumerate(["simple"] * 30 + ["moderate"] * 50 + ["challenging"] * 20)
    ]
    a = stratified_sample(qs, 20, seed=0)
    b = stratified_sample(qs, 20, seed=0)
    assert [q["question_id"] for q in a] == [q["question_id"] for q in b]
    diff = [q["difficulty"] for q in a]
    assert (diff.count("simple"), diff.count("moderate"), diff.count("challenging")) == (6, 10, 4)


def test_wilson_interval():
    lo, hi = wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    assert wilson(0, 0) == (0.0, 0.0)


def test_summarize_groups_by_config():
    rows = [
        {
            "model": "haiku",
            "pipeline": "baseline",
            "effort": "medium",
            "repeat": 0,
            "difficulty": "simple",
            "correct": c,
            "status": "ok",
            "cost_usd": 0.001,
            "latency_s": 1.0,
        }
        for c in (True, True, False, True)
    ]
    s = summarize(rows)
    assert s[0]["n"] == 4 and s[0]["ex"] == 0.75
    assert s[0]["cost_per_q"] == pytest.approx(0.001)


def test_mcnemar_exact():
    from evals.compare import mcnemar_exact

    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(0, 10) < 0.01  # 10 题只在 B 对：显著
    assert 0.2 < mcnemar_exact(3, 7) < 0.4


def test_compare_counts_corrections():
    from evals.compare import compare

    def row(i, correct, first_correct=None, first_sql=None, sql="S"):
        return {
            "question_id": i,
            "model": "m",
            "correct": correct,
            "cost_usd": 0.0,
            "pred_sql": sql,
            "first_sql": first_sql or sql,
            "first_correct": correct if first_correct is None else first_correct,
        }

    a = [row(1, False), row(2, True), row(3, True)]
    b = [row(1, True, False, "OLD"), row(2, False, True, "OLD"), row(3, True)]
    out = compare(a, b, "m")
    assert out["b_only_correct"] == [1] and out["a_only_correct"] == [2]
    assert out["corrections"]["fixed"] == 1 and out["corrections"]["broke"] == 1


def test_factory_routes_local_and_claude_and_shares_clients(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from datapilot.llm import ClaudeLLM, LocalLLM
    from evals.runner import make_factory

    f = make_factory(False)
    q = {"question_id": 1}
    a, b = (
        f(Config("qwen7b", "e4_correct", "medium", 0), q),
        f(Config("qwen7b", "e4_correct", "medium", 0), q),
    )
    c = f(Config("haiku", "e4_correct", "medium", 0), q)
    assert isinstance(a, LocalLLM) and isinstance(c, ClaudeLLM)
    assert a.client is b.client  # 共用一个 HTTP 客户端


def test_validate_rejects_unknown_model_and_local_agent():
    import pytest

    from evals.runner import validate

    with pytest.raises(SystemExit, match="未知模型"):
        validate(["sonet"], "e4_correct")
    with pytest.raises(SystemExit, match="固定流程"):
        validate(["qwen7b"], "agent")
    validate(["haiku", "qwen7b"], "e4_correct")
    validate(["gold"], "agent")


def test_main_accepts_custom_question_file(tmp_path, monkeypatch, shop_db_dir):
    """--questions 指向自定义题集：接入新库时用标准答案空跑全管线必须 100%。"""
    import json as _json

    from evals import runner as runner_mod

    qfile = tmp_path / "shop.json"
    qfile.write_text(
        _json.dumps(
            [
                {
                    "question_id": 1,
                    "db_id": "shop",
                    "question": "多少用户",
                    "evidence": "",
                    "SQL": "SELECT count(*) FROM users",
                    "difficulty": "simple",
                },
                {
                    "question_id": 2,
                    "db_id": "shop",
                    "question": "女性用户",
                    "evidence": "",
                    "SQL": "SELECT name FROM users WHERE gender = 'F'",
                    "difficulty": "simple",
                },
            ]
        )
    )
    monkeypatch.setattr("datapilot.config.RUNS_DIR", tmp_path / "runs")
    runner_mod.main(
        [
            "--models",
            "gold",
            "--pipeline",
            "e4_correct",
            "--questions",
            str(qfile),
            "--tag",
            "custom",
        ]
    )
    run_dir = next((tmp_path / "runs").iterdir())
    summary = _json.loads((run_dir / "summary.json").read_text())
    assert summary[0]["n"] == 2 and summary[0]["ex"] == 1.0


def test_resume_with_different_question_set_is_refused(tmp_path, monkeypatch, shop_db_dir):
    import json as _json

    import pytest

    from evals import runner as runner_mod

    def qfile(name, ids):
        p = tmp_path / name
        p.write_text(
            _json.dumps(
                [
                    {
                        "question_id": i,
                        "db_id": "shop",
                        "question": "q",
                        "evidence": "",
                        "SQL": "SELECT 1",
                        "difficulty": "simple",
                    }
                    for i in ids
                ]
            )
        )
        return str(p)

    monkeypatch.setattr("datapilot.config.RUNS_DIR", tmp_path / "runs")
    runner_mod.main(["--models", "gold", "--questions", qfile("a.json", [1, 2]), "--tag", "t"])
    run_id = next((tmp_path / "runs").iterdir()).name
    with pytest.raises(SystemExit, match="题集"):
        runner_mod.main(
            ["--models", "gold", "--questions", qfile("b.json", [1, 2, 3]), "--resume", run_id]
        )


def test_api_models_validate_factory_and_keys(monkeypatch):
    """国产 API 模型：只跑固定流程；必须有价格；调用前必须有密钥；工厂返回 OpenAICompatLLM。"""
    import pytest

    from datapilot.llm import OpenAICompatLLM
    from evals.runner import Config, check_keys, make_factory, validate

    validate(["deepseek", "qwen"], "e4_correct")
    with pytest.raises(SystemExit, match="固定流程"):
        validate(["deepseek"], "agent")
    monkeypatch.setenv("QWEN_MODEL", "qwen-unpriced")
    with pytest.raises(SystemExit, match="没有价格"):
        validate(["qwen"], "e4_correct")
    monkeypatch.delenv("QWEN_MODEL")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="DEEPSEEK_API_KEY"):
        check_keys(["haiku", "deepseek"])
    check_keys(["haiku"])

    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    llm = make_factory(False)(Config("qwen", "e4_correct", "medium", 0), {})
    assert isinstance(llm, OpenAICompatLLM)
    assert llm.model == "qwen3.7-plus" and llm.extra_body == {"enable_thinking": False}
    assert llm.base_url.startswith("https://dashscope.aliyuncs.com")


def test_estimate_prices_api_models():
    from datapilot import config
    from evals.runner import Config, estimate

    q = [{"question_id": 1, "db_id": "superhero", "question": "x", "evidence": ""}]
    if not config.db_path("superhero").exists():
        import pytest

        pytest.skip("需要 BIRD 数据")
    est = estimate(q, [Config("deepseek", "e4_correct", "medium", 0)])
    assert 0 < est < 0.01


def test_run_one_reraises_fatal_api_error(shop_db_dir):
    """账户错误不能被吞成 llm_error：否则额度用完后剩下的题全记成失败（v1.3 百炼实测）。"""
    import asyncio as _asyncio

    import pytest

    from datapilot.llm import FatalAPIError
    from evals.runner import Config, run_one

    class Boom:
        model = "x"

        async def complete(self, *a, **k):
            raise FatalAPIError("HTTP 403")

    q = {
        "question_id": 1,
        "db_id": "shop",
        "difficulty": "simple",
        "question": "q",
        "evidence": "",
        "SQL": "SELECT 1",
    }
    with pytest.raises(FatalAPIError):
        _asyncio.run(run_one(q, Config("qwen", "baseline", "medium", 0), Boom()))
