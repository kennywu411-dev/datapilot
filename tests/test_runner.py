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
