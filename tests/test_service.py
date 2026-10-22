"""问数服务：流式事件、追问、查询缓存、自动图表。"""

from datapilot.baseline import SQLAnswer
from datapilot.llm import ScriptedLLM
from datapilot.service import ChatService, choose_chart, normalize_question
from datapilot.store import MemoryStore

# ---------- 规范化与图表 ----------


def test_normalize_question():
    assert normalize_question("  前 5 名  的用户？ ") == normalize_question("前 5 名 的用户")
    assert normalize_question("Top５ Users!") == "top5 users"  # 全角转半角、去尾标点、小写


def test_chart_line_for_time_series():
    c = choose_chart(["year", "total"], [("2019", 3), ("2020", 5), ("2021", 4)], "趋势")
    assert c == {"type": "line", "x": "year", "y": ["total"]}


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
    session = await svc.store.get_json("dp:session:s1")
    assert [t["question"] for t in session["turns"]] == ["波士顿有多少用户？", "那纽约呢？"]


async def test_switching_db_resets_follow_up_context(shop_db_dir):
    svc, llm = make_service([SQLAnswer(sql="SELECT 1"), "1。", SQLAnswer(sql="SELECT 2"), "2。"])
    await collect(svc.ask("s1", "shop", "问题一"))
    session = await svc.store.get_json("dp:session:s1")
    session["db_id"] = "other"
    await svc.store.set_json("dp:session:s1", session, ttl_s=60)
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
