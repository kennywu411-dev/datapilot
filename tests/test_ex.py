"""EX 比对测试：口径与官方 evaluation_ex.py 对齐（set(pred) == set(gold)）。"""

import sqlite3

import pytest

from datapilot.ex import ExOutcome, evaluate_one, ex_match


def test_order_does_not_matter():
    assert ex_match([(1,), (2,)], [(2,), (1,)])


def test_duplicates_ignored_like_official():
    # 官方用 set 比较，重复行不影响结果；为了可比，保留这个口径
    assert ex_match([(1,), (1,)], [(1,)])


def test_different_values_fail():
    assert not ex_match([(1,)], [(2,)])


def test_column_order_matters():
    # 官方比较整行元组，列顺序不同算错
    assert not ex_match([(1, "a")], [("a", 1)])


def test_int_vs_float_equal_in_python_set():
    # Python 中 1 == 1.0 且 hash 相同，官方 set 比较判为相等；我们保持一致
    assert ex_match([(1,)], [(1.0,)])


def test_empty_vs_empty():
    assert ex_match([], [])


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b"), (3, "c")])
    conn.commit()
    conn.close()
    return path


GOLD = "SELECT id FROM t WHERE id >= 2"


def test_evaluate_correct(db):
    out = evaluate_one(db, "SELECT id FROM t WHERE id > 1 ORDER BY id DESC", GOLD)
    assert out == ExOutcome(correct=True, status="ok")


def test_evaluate_wrong(db):
    out = evaluate_one(db, "SELECT id FROM t", GOLD)
    assert out.correct is False and out.status == "ok"


def test_evaluate_pred_error(db):
    out = evaluate_one(db, "SELECT nope FROM t", GOLD)
    assert out.correct is False and out.status == "pred_error"


def test_evaluate_rejected_write_does_not_touch_db(db):
    out = evaluate_one(db, "DROP TABLE t", GOLD)
    assert out.correct is False and out.status == "pred_rejected"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (3,)
    conn.close()


def test_evaluate_empty_pred(db):
    out = evaluate_one(db, "", GOLD)
    assert out.correct is False and out.status == "pred_rejected"


def test_evaluate_timeout(db):
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    out = evaluate_one(db, slow, GOLD, timeout_s=0.3)
    assert out.correct is False and out.status == "pred_timeout"


def test_evaluate_gold_error_is_flagged(db):
    out = evaluate_one(db, "SELECT 1", "SELECT nope FROM t")
    assert out.correct is False and out.status == "gold_error"


def test_pred_and_gold_share_one_time_budget(db, monkeypatch):
    """官方 func_timeout 把预测和标准 SQL 包在同一个 30 秒里：先跑预测，标准只能用剩余时间。"""
    import datapilot.ex as ex_mod
    from datapilot.sql_guard import QueryResult

    calls = []

    def fake_run(path, sql, *, timeout_s, **kw):
        calls.append((sql, timeout_s))
        return QueryResult(["x"], [(1,)], False, 20.0 if sql == "PRED" else 0.0)

    monkeypatch.setattr(ex_mod, "run_readonly", fake_run)
    evaluate_one(db, "PRED", "GOLD", timeout_s=30.0)
    assert [c[0] for c in calls] == ["PRED", "GOLD"]
    assert calls[0][1] == 30.0
    assert calls[1][1] == pytest.approx(10.0)


def test_gold_timeout_after_pred_uses_budget(db, monkeypatch):
    import datapilot.ex as ex_mod
    from datapilot.sql_guard import QueryResult

    def fake_run(path, sql, *, timeout_s, **kw):
        if sql == "GOLD":
            raise TimeoutError("slow")
        return QueryResult(["x"], [(1,)], False, 29.0)

    monkeypatch.setattr(ex_mod, "run_readonly", fake_run)
    out = evaluate_one(db, "PRED", "GOLD", timeout_s=30.0)
    assert out.correct is False and out.status == "timeout"
