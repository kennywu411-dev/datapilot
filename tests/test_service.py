"""问数服务：流式事件、追问、查询缓存、自动图表。"""

from datapilot.baseline import SQLAnswer
from datapilot.llm import ScriptedLLM
from datapilot.service import ChatService, choose_chart, normalize_question
from datapilot.store import MemoryStore

# ---------- 规范化与图表 ----------


def test_normalize_question():
    assert normalize_question("  前 5 名  的用户？ ") == normalize_question("前 5 名 的用户")
    assert normalize_question("Top５ Users!") == "top5 users"  # 全角转半角、去尾标点、小写


def test_normalize_keeps_case_inside_quotes():
    """引号里多是库里的取值，区分大小写：'F' 和 'f' 不能命中同一条缓存。"""
    assert normalize_question("Gender = 'F' 有多少人") != normalize_question(
        "Gender = 'f' 有多少人"
    )
    assert normalize_question("WHO IS 'chl'") == normalize_question("who is 'chl'")
    assert normalize_question('List "Marvel Comics" heroes') == 'list "Marvel Comics" heroes'


def test_chart_line_for_time_series():
    c = choose_chart(["year", "total"], [("2019", 3), ("2020", 5), ("2021", 4)], "趋势")
    assert c == {"type": "line", "x": "year", "y": ["total"]}


def test_chart_line_when_year_is_integer_column():
    """年份常存成整数：两列都是数值时，列名像时间的那列当 x 轴。"""
    c = choose_chart(["year", "races"], [(1950, 7), (1951, 8), (1952, 8)], "每年多少场")
    assert c == {"type": "line", "x": "year", "y": ["races"]}


def test_chart_bar_for_category_numeric():
    c = choose_chart(["city", "n"], [("Boston", 2), ("NY", 1), ("LA", 4), ("SF", 3)], "各城市人数")
    assert c["type"] == "bar" and c["x"] == "city"


def test_chart_pie_for_share_with_few_categories():
    c = choose_chart(["gender", "n"], [("F", 2), ("M", 1)], "男女占比")
    assert c["type"] == "pie"


def test_chart_pie_uses_first_numeric_when_share_has_count_and_percent():
    """问占比时结果常同时带人数和百分比两列：饼图只取第一列（人数），百分比由饼图自己算。"""
    c = choose_chart(
        ["gender", "n", "pct"], [("Male", 252, 69.4), ("Female", 111, 30.6)], "男女占比如何"
    )
    assert c == {"type": "pie", "x": "gender", "y": ["n"]}


def test_chart_none_for_scalar_or_text():
    assert choose_chart(["n"], [(3,)], "多少人")["type"] == "kpi"
    assert choose_chart(["name"], [("a",), ("b",)], "有哪些")["type"] == "table"
    assert choose_chart(["n"], [], "多少")["type"] == "table"


def test_answer_prompt_states_row_counts():
    """回答阶段要知道结果总行数与给出的行数，否则会误以为数据只有这么多（P11）。"""
    from datapilot.service import ANSWER_PREVIEW_ROWS, ChatService

    table = {
        "columns": ["year", "n"],
        "rows": [[1950 + i, i] for i in range(68)],
        "truncated": False,
    }
    p = ChatService._answer_prompt("每年多少场", "SELECT ...", table)
    assert "共 68 行" in p and "全部" in p
    assert ANSWER_PREVIEW_ROWS >= 68
    big = {"columns": ["x"], "rows": [[i] for i in range(300)], "truncated": False}
    p2 = ChatService._answer_prompt("q", "SELECT ...", big)
    assert f"前 {ANSWER_PREVIEW_ROWS} 行" in p2 and "共 300 行" in p2


# ---------- 服务 ----------


async def collect(gen):
    return [e async for e in gen]


def make_service(script, store=None):
    llm = ScriptedLLM(script, model="claude-haiku-5-5")
    return ChatService(store or MemoryStore(), llm_factory=lambda: llm), llm


async def test_ask_streams_steps_sql_table_chart_answer(shop_db_dir):
    svc, llm = make_service(
        [
            SQLAnswer(sql="SELECT city, count(*) AS n FROM users GROUP BY city"),
            "波士顿 2 人，纽约 1 人。",
        ]
    )
    events = await collect(svc.ask("s1", "shop", "各城市有多少用户？"))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "step" and "sql" in kinds and "table" in kinds and "chart" in kinds
    assert kinds[-2:] == ["answer", "done"]
    table = next(e for e in events if e["event"] == "table")["data"]
    assert table["columns"] == ["city", "n"] and len(table["rows"]) == 2
    assert next(e for e in events if e["event"] == "done")["data"]["cached"] is False


async def test_second_identical_question_hits_cache(shop_db_dir):
    svc, llm = make_service([SQLAnswer(sql="SELECT count(*) FROM users"), "共 3 人。"])
    await collect(svc.ask("s1", "shop", "一共多少用户？"))
    calls = len(llm.calls)
    events = await collect(svc.ask("s2", "shop", "一共多少用户"))  # 另一个会话、少个问号
    assert len(llm.calls) == calls  # 没有再调用模型
    done = next(e for e in events if e["event"] == "done")["data"]
    assert done["cached"] is True
    assert next(e for e in events if e["event"] == "answer")["data"]["text"] == "共 3 人。"


async def test_follow_up_passes_previous_sql_and_is_not_cache_shared(shop_db_dir):
    svc, llm = make_service(
        [
            SQLAnswer(sql="SELECT count(*) FROM users WHERE city = 'Boston'"),
            "2 人。",
            SQLAnswer(sql="SELECT count(*) FROM users WHERE city = 'New York'"),
            "1 人。",
        ]
    )
    await collect(svc.ask("s1", "shop", "波士顿有多少用户？"))
    await collect(svc.ask("s1", "shop", "那纽约呢？"))
    follow_up_user_msg = llm.calls[2]["messages"][0]["content"]
    assert "波士顿有多少用户" in follow_up_user_msg
    assert "city = 'Boston'" in follow_up_user_msg
    session = await svc.store.get_json("dp:session:anon:s1")
    assert [t["question"] for t in session["turns"]] == ["波士顿有多少用户？", "那纽约呢？"]


async def test_switching_db_resets_follow_up_context(shop_db_dir):
    svc, llm = make_service([SQLAnswer(sql="SELECT 1"), "1。", SQLAnswer(sql="SELECT 2"), "2。"])
    await collect(svc.ask("s1", "shop", "问题一"))
    session = await svc.store.get_json("dp:session:anon:s1")
    session["db_id"] = "other"
    await svc.store.set_json("dp:session:anon:s1", session, ttl_s=60)
    await collect(svc.ask("s1", "shop", "问题二"))
    assert "Previous" not in llm.calls[2]["messages"][0]["content"]


async def test_error_event_when_sql_fails_after_corrections(shop_db_dir):
    svc, _ = make_service(
        [SQLAnswer(sql=f"SELECT bad{i} FROM users") for i in range(4)] + ["无法回答。"]
    )
    events = await collect(svc.ask("s1", "shop", "随便问"))
    assert any(e["event"] == "error" for e in events)
    assert events[-1]["event"] == "done"


async def test_unknown_db_is_error(shop_db_dir):
    svc, _ = make_service([])
    events = await collect(svc.ask("s1", "nope", "q"))
    assert events[0]["event"] == "error"


async def test_final_sql_executed_once_and_pipeline_rows_reused(shop_db_dir, monkeypatch):
    """流程里已经执行过最终 SQL，服务层不应再执行一遍（慢查询延迟会翻倍）。"""
    import datapilot.sql_guard as guard

    calls = []
    real = guard.run_readonly

    def counting(path, sql, **kw):
        calls.append(sql)
        return real(path, sql, **kw)

    monkeypatch.setattr("datapilot.pipeline.run_readonly", counting)
    # 服务层已不再自己执行 SQL（没有 run_readonly 引用），只需统计流程里的执行次数
    svc, _ = make_service(
        [SQLAnswer(sql="SELECT city, count(*) AS n FROM users GROUP BY city"), "好。"]
    )
    events = await collect(svc.ask("s1", "shop", "各城市多少人"))
    assert calls.count("SELECT city, count(*) AS n FROM users GROUP BY city") == 1
    table = next(e for e in events if e["event"] == "table")["data"]
    assert len(table["rows"]) == 2


async def test_failed_turn_is_remembered_for_follow_up(shop_db_dir):
    svc, llm = make_service(
        [SQLAnswer(sql=f"SELECT bad{i} FROM users") for i in range(4)]
        + [SQLAnswer(sql="SELECT count(*) FROM users"), "3 人。"]
    )
    await collect(svc.ask("s1", "shop", "第一问"))
    await collect(svc.ask("s1", "shop", "刚才报错了，换个写法"))
    follow_up = llm.calls[4]["messages"][0]["content"]
    assert "第一问" in follow_up and "failed" in follow_up


async def test_concurrent_asks_in_same_session_keep_all_turns(shop_db_dir):
    import asyncio

    svc, _ = make_service([SQLAnswer(sql="SELECT 1"), "一。", SQLAnswer(sql="SELECT 2"), "二。"])
    await asyncio.gather(
        collect(svc.ask("s1", "shop", "问一")), collect(svc.ask("s1", "shop", "问二"))
    )
    session = await svc.store.get_json("dp:session:anon:s1")
    assert sorted(t["question"] for t in session["turns"]) == ["问一", "问二"]


def test_default_factory_shares_one_client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    svc = ChatService(MemoryStore())
    assert svc.llm_factory().client is svc.llm_factory().client


# ---------- 多用户与并发保护 ----------


async def test_sessions_are_isolated_per_user(shop_db_dir):
    svc, llm = make_service([SQLAnswer(sql="SELECT 1"), "一。", SQLAnswer(sql="SELECT 2"), "二。"])
    await collect(svc.ask("s1", "shop", "爱丽丝的私密问题", user_id="alice"))
    await collect(svc.ask("s1", "shop", "鲍勃的问题", user_id="bob"))  # 同一个 session_id
    bob_msg = llm.calls[2]["messages"][0]["content"]
    assert "爱丽丝" not in bob_msg  # 拿不到别人的追问上下文
    alice = await svc.store.get_json("dp:session:alice:s1")
    bob = await svc.store.get_json("dp:session:bob:s1")
    assert [t["question"] for t in alice["turns"]] == ["爱丽丝的私密问题"]
    assert [t["question"] for t in bob["turns"]] == ["鲍勃的问题"]


async def test_inflight_limit_rejects_when_queue_times_out(shop_db_dir):
    import asyncio

    from datapilot.llm import SimulatedLLM

    svc = ChatService(
        MemoryStore(),
        llm_factory=lambda: SimulatedLLM(latency_s=0.3),
        max_inflight=1,
        queue_timeout_s=0.05,
    )
    first, second = await asyncio.gather(
        collect(svc.ask("s1", "shop", "问一", user_id="a")),
        collect(svc.ask("s2", "shop", "问二", user_id="b")),
    )
    assert [e["event"] for e in first][-2:] == ["answer", "done"]
    err = next(e for e in second if e["event"] == "error")["data"]
    assert "繁忙" in err["message"]
    assert second[-1]["event"] == "done"


async def test_cache_hits_bypass_inflight_limit(shop_db_dir):
    import asyncio

    from datapilot.llm import SimulatedLLM

    svc = ChatService(
        MemoryStore(),
        llm_factory=lambda: SimulatedLLM(latency_s=0.3),
        max_inflight=1,
        queue_timeout_s=0.05,
    )
    await collect(svc.ask("s0", "shop", "热门问题", user_id="a"))  # 先写进缓存
    hit, _ = await asyncio.gather(
        collect(svc.ask("s1", "shop", "热门问题", user_id="b")),
        collect(svc.ask("s2", "shop", "冷门问题", user_id="c")),  # 占住唯一的名额
    )
    assert hit[-1]["data"]["cached"] is True


async def test_queued_request_gets_queue_step_immediately(shop_db_dir):
    import asyncio

    from datapilot.llm import SimulatedLLM

    svc = ChatService(
        MemoryStore(),
        llm_factory=lambda: SimulatedLLM(latency_s=0.2),
        max_inflight=1,
        queue_timeout_s=5,
    )
    first = asyncio.create_task(collect(svc.ask("s1", "shop", "问一", user_id="a")))
    while not svc._inflight.locked():  # 等第一个请求占住名额，不靠固定 sleep
        await asyncio.sleep(0.005)
    gen = svc.ask("s2", "shop", "问二", user_id="b")
    head = await asyncio.wait_for(gen.__anext__(), 0.1)  # 不用等名额就先收到排队事件
    assert head["event"] == "step" and head["data"]["name"] == "queue"
    rest = [e async for e in gen]
    assert rest[-2]["event"] == "answer"
    await first


async def test_no_queue_step_when_slot_is_free(shop_db_dir):
    from datapilot.llm import SimulatedLLM

    svc = ChatService(MemoryStore(), llm_factory=lambda: SimulatedLLM(latency_s=0))
    events = await collect(svc.ask("s1", "shop", "问一"))
    assert all(e["data"].get("name") != "queue" for e in events if e["event"] == "step")


async def test_queued_request_rechecks_cache_after_getting_slot(shop_db_dir):
    import asyncio

    from datapilot.llm import SimulatedLLM

    made = []

    def factory():
        made.append(1)
        return SimulatedLLM(latency_s=0.2)

    svc = ChatService(MemoryStore(), llm_factory=factory, max_inflight=1, queue_timeout_s=5)
    a, b = await asyncio.gather(
        collect(svc.ask("s1", "shop", "同一个热门问题", user_id="a")),
        collect(svc.ask("s2", "shop", "同一个热门问题", user_id="b")),
    )
    assert len(made) == 1  # 第二个请求排完队直接用缓存，没有再调模型
    assert {a[-1]["data"]["cached"], b[-1]["data"]["cached"]} == {False, True}


def test_max_inflight_must_be_positive():
    import pytest

    with pytest.raises(ValueError):
        ChatService(MemoryStore(), max_inflight=0)
