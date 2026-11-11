"""指标异动归因：问题识别、计划校验、贡献度分解、下钻与剥离、数字核对、权限、服务接入。

用一个和 retail_ops 同结构的小库：5 月和 6 月的数据完全相同（总变化为 0），
每个测试再往里注入一个已知原因，所以断言的数字都能手算。
"""

import asyncio
import sqlite3

import pytest

from datapilot import attribution as at
from datapilot.attribution import AttrConfig, AttributionPlan, Scope, run_attribution
from datapilot.baseline import SQLAnswer
from datapilot.llm import ScriptedLLM
from datapilot.numcheck import check_numbers, extract_numbers
from datapilot.semantic import DimFilter, load_layer
from datapilot.service import ChatService
from datapilot.sql_guard import check_sql
from datapilot.store import MemoryStore
from scripts.build_retail_ops import DDL, categorize, size_band

MAY, JUN = ("2011-05-01", "2011-06-01"), ("2011-06-01", "2011-07-01")
COUNTRIES = [
    ("United Kingdom", "英国", "UK", 20),
    ("Germany", "德国", "Overseas", 4),
    ("France", "法国", "Overseas", 2),
]
BASE_GMV = 26 * 300.0  # 每月 26 张销售单，每张 3 行 × 100
CATS = ["家居装饰", "包袋收纳", "节日派对"]


def _fill(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    for c, zh, market, _ in COUNTRIES:
        conn.execute("INSERT INTO countries VALUES (?, ?, ?)", (c, zh, market))
    for s in ("11111", "22222", "33333"):
        conn.execute("INSERT INTO products VALUES (?, ?)", (s, f"ITEM {s}"))
    n = 0
    for month in ("05", "06"):
        for country, _, _, count in COUNTRIES:
            for i in range(count):
                n += 1
                cust = 10001 + (i % 5) + 100 * [c[0] for c in COUNTRIES].index(country)
                conn.execute(
                    "INSERT OR IGNORE INTO customers VALUES (?, ?, '2011-05-02', ?)",
                    (cust, country, "A" if i % 5 == 0 else "B"),
                )
                no = f"5{n:05d}"
                conn.execute(
                    "INSERT INTO invoices VALUES (?, 'sale', ?, ?, ?, 'returning', 'small', 300.0)",
                    (no, f"2011-{month}-{2 + i % 20:02d} 10:00:00", cust, country),
                )
                for k, cat in enumerate(CATS):
                    conn.execute(
                        "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, "
                        "unit_price, amount) VALUES (?, ?, ?, 10, 10.0, 100.0)",
                        (no, ("11111", "22222", "33333")[k], cat),
                    )
        # 每月一张小额退货单
        conn.execute(
            "INSERT INTO invoices VALUES (?, 'return', ?, 10001, 'United Kingdom', 'returning', "
            "'small', 72.0)",
            (f"C{month}0001", f"2011-{month}-15 09:00:00"),
        )
        conn.execute(
            "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, unit_price, "
            "amount) VALUES (?, '11111', '家居装饰', 6, 12.0, 72.0)",
            (f"C{month}0001",),
        )
    conn.commit()


@pytest.fixture
def retail(tmp_path, monkeypatch):
    d = tmp_path / "dbs" / "retail_ops"
    d.mkdir(parents=True)
    path = d / "retail_ops.sqlite"
    conn = sqlite3.connect(path)
    _fill(conn)
    conn.close()
    monkeypatch.setattr("datapilot.config.DB_DIR", tmp_path / "dbs")
    return path


def alter(path, *statements):
    conn = sqlite3.connect(path)
    for s in statements:
        conn.execute(s)
    conn.commit()
    conn.close()


@pytest.fixture
def layer():
    return load_layer("retail_ops")


def run(path, metric, layer, role=None, **cfg):
    return run_attribution(layer, path, Scope(metric, MAY, JUN), role, AttrConfig(**cfg))


# ---------- 问题识别 ----------


@pytest.mark.parametrize(
    "q",
    [
        "上周销售额为什么掉了",
        "2011 年 1 月退货率怎么回事，比上个月高这么多",
        "6 月 GMV 下降的原因是什么",
        "客单价下滑是什么导致的？",
        "帮我做一下 11 月净销售额的归因",
        "订单数为啥少了",
    ],
)
def test_why_questions_are_detected(q):
    assert at.is_why_question(q)


@pytest.mark.parametrize(
    "q",
    [
        "2011 年 1 月的退货率是多少",
        "各售后原因的工单数分别是多少",  # 电商库有"售后原因"这个维度，不能一见"原因"就走归因
        "按国家统计近 12 个月的 GMV",
        "哪个品类的退货额最高",
    ],
)
def test_plain_questions_are_not_detected(q):
    assert not at.is_why_question(q)


def test_only_databases_with_attribution_config_are_supported(layer):
    assert at.supports(layer)
    assert not at.supports(load_layer("shop_ops"))
    assert not at.supports(None)


# ---------- 字典与计划 ----------


def test_attributable_metrics_exclude_distinct_counts_and_formula_less_ratios(layer):
    ids = {m.id for m in at.attributable_metrics(layer)}
    assert {"gmv", "return_rate", "net_sales", "aov", "order_count"} <= ids
    assert "customers" not in ids  # COUNT(DISTINCT) 不能按分组相加
    assert "repeat_rate" not in ids  # 没有 formula


def plan(**kw):
    base = dict(
        ok=True,
        metric="gmv",
        current_start="2011-06-01",
        current_end="2011-06-30",
        base_start="2011-05-01",
        base_end="2011-05-31",
    )
    return AttributionPlan(**{**base, **kw})


def test_resolve_plan_makes_half_open_periods(layer):
    s = at.resolve_plan(layer, plan(filters=[DimFilter(dimension="country", values=["Germany"])]))
    assert s.base == MAY and s.current == JUN
    assert s.filters == (("country", ("Germany",)),)


@pytest.mark.parametrize(
    "kw, message",
    [
        (dict(ok=False, reason="不是归因问题"), "不是归因问题"),
        (dict(metric="nope"), "没有 nope"),
        (dict(metric="customers"), "暂不支持归因"),
        (dict(current_start="2011-13-01"), "不是合法日期"),
        (dict(base_end="2011-06-10"), "重叠"),
        (dict(current_end="2011-05-01"), "结束早于开始"),
        (dict(filters=[DimFilter(dimension="nope", values=["x"])]), "不存在的维度"),
    ],
)
def test_resolve_plan_rejects_bad_plans(layer, kw, message):
    with pytest.raises(at.PlanError, match=message):
        at.resolve_plan(layer, plan(**kw))


def test_plan_prompt_lists_metrics_dates_and_stored_values(layer):
    p = at.plan_prompt(layer)
    assert "Today is 2011-12-01" in p and "return_rate (退货率" in p
    assert "Germany=德国" in p and "customers (客户数" not in p


# ---------- 分解 ----------


def test_identical_periods_are_not_attributed(retail, layer):
    r = run(retail, "gmv", layer)
    assert r.total["change"] == 0 and r.causes == []
    assert "基本没变" in r.stop_reason


def test_country_drop_is_found_and_contributions_add_up(retail, layer):
    # 删掉德国 6 月的全部销售单：GMV 少 4 × 300 = 1200
    alter(
        retail,
        "DELETE FROM invoice_lines WHERE invoice_no IN (SELECT invoice_no FROM invoices "
        "WHERE country = 'Germany' AND invoice_date >= '2011-06-01')",
        "DELETE FROM invoices WHERE country = 'Germany' AND invoice_date >= '2011-06-01'",
    )
    r = run(retail, "gmv", layer)
    assert r.total["base"] == BASE_GMV and r.total["change"] == -1200
    top = r.causes[0]
    assert [(c["dimension"], c["value"]) for c in top.conditions] == [("country", "Germany")]
    assert top.contribution == pytest.approx(1.0)
    assert top.expected == pytest.approx(1200 / BASE_GMV)  # 德国在基期 GMV 里的份额
    for b in r.breakdowns:
        if b.kind == "dimension":  # 每个维度的各组贡献相加都等于 1
            assert sum(s.contribution for s in b.segments) == pytest.approx(1.0)
    assert "100.0%" in r.stop_reason and len(r.causes) == 1


def test_raw_ranking_prefers_the_big_segment_excess_prefers_the_surprising_one(retail, layer):
    # 英国每个品类各少一点（同比例），德国整体消失：按原始贡献英国和德国一样大，按超额是德国
    alter(
        retail,
        "DELETE FROM invoice_lines WHERE invoice_no IN (SELECT invoice_no FROM invoices "
        "WHERE country = 'Germany' AND invoice_date >= '2011-06-01')",
        "DELETE FROM invoices WHERE country = 'Germany' AND invoice_date >= '2011-06-01'",
        "UPDATE invoice_lines SET amount = 80.0, unit_price = 8.0 WHERE invoice_no IN "
        "(SELECT invoice_no FROM invoices WHERE country = 'United Kingdom' "
        "AND invoice_type = 'sale' AND invoice_date >= '2011-06-01')",
    )
    excess = run(retail, "gmv", layer, entities=False, max_depth=1)
    raw = run(retail, "gmv", layer, entities=False, max_depth=1, ranking="raw")
    assert excess.causes[0].conditions[0]["value"] == "Germany"
    assert raw.causes[0].conditions[0]["value"] != "Germany"  # 排到了本来就占大头的分组
    assert raw.causes[0].contribution >= excess.causes[0].contribution


def test_large_return_invoice_is_traced_to_the_document(retail, layer):
    alter(
        retail,
        "INSERT INTO invoices VALUES ('C999', 'return', '2011-06-20 10:00:00', 10003, "
        "'United Kingdom', 'returning', 'large', 6000.0)",
        "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, unit_price, "
        "amount) VALUES ('C999', '22222', '包袋收纳', 600, 10.0, 6000.0)",
    )
    r = run(retail, "return_rate", layer)
    # 退货率 = 退货额 ÷ GMV：72 / 7800 → 6072 / 7800
    assert r.total["base"] == pytest.approx(72 / BASE_GMV)
    assert r.total["current"] == pytest.approx(6072 / BASE_GMV)
    top = r.causes[0]
    assert top.kind == "document" and top.conditions[0]["value"] == "C999"
    assert top.detail["amount"] == 6000.0 and top.detail["customer_id"] == 10003
    facts = {f.label: f for f in r.facts}
    without = next(v for k, v in facts.items() if k.startswith("剔除原因 1"))
    assert without.value == pytest.approx(72 / BASE_GMV)  # 拿掉这张单，本期退货率回到基期水平
    assert without.display == "0.92%"


def test_drill_refines_to_a_cross_of_two_dimensions(retail, layer):
    # 只删德国 6 月的"节日派对"行：第一层是德国，往下拆发现集中在节日派对，原因细化成交叉
    alter(
        retail,
        "DELETE FROM invoice_lines WHERE category = '节日派对' AND invoice_no IN "
        "(SELECT invoice_no FROM invoices WHERE country = 'Germany' "
        "AND invoice_type = 'sale' AND invoice_date >= '2011-06-01')",
    )
    deep = run(retail, "gmv", layer, entities=False)
    assert {(c["dimension"], c["value"]) for c in deep.causes[0].conditions} == {
        ("category", "节日派对"),
        ("country", "Germany"),
    }
    assert deep.causes[0].note == "占 国家=德国 变化的 100.0%" and deep.drill
    assert deep.causes[0].contribution == pytest.approx(1.0)
    flat = run(retail, "gmv", layer, entities=False, max_depth=1)
    assert len(flat.causes[0].conditions) == 1 and not flat.drill


def test_child_that_lies_entirely_inside_its_parent_is_not_written_as_a_cross(retail, layer):
    # 德国少 1200、法国少 150：第一层是 市场=海外；往下拆到 国家=德国 时，德国整个落在海外之内，
    # 原因写成"国家=德国"，不写成"海外 × 德国"
    alter(
        retail,
        "DELETE FROM invoice_lines WHERE invoice_no IN (SELECT invoice_no FROM invoices "
        "WHERE country = 'Germany' AND invoice_date >= '2011-06-01')",
        "UPDATE invoice_lines SET amount = 75.0 WHERE invoice_no IN (SELECT invoice_no "
        "FROM invoices WHERE country = 'France' AND invoice_date >= '2011-06-01')",
    )
    cfg = dict(entities=False, dimensions=("market", "country"), peel=False, max_causes=1)
    kept = run(retail, "gmv", layer, **cfg)
    assert [c["value"] for c in kept.causes[0].conditions] == ["Overseas"]  # 默认门槛下不细化
    r = run(retail, "gmv", layer, promote_score=0.1, **cfg)
    assert [(c["dimension"], c["value"]) for c in r.causes[0].conditions] == [
        ("country", "Germany")
    ]


def test_peeling_finds_a_second_independent_cause(retail, layer):
    alter(
        retail,
        # 原因 1：德国 6 月整体消失（−1200）；原因 2：其余国家的包袋收纳降价一半（−1100）
        "DELETE FROM invoice_lines WHERE invoice_no IN (SELECT invoice_no FROM invoices "
        "WHERE country = 'Germany' AND invoice_date >= '2011-06-01')",
        "DELETE FROM invoices WHERE country = 'Germany' AND invoice_date >= '2011-06-01'",
        "UPDATE invoice_lines SET amount = 50.0, unit_price = 5.0 WHERE category = '包袋收纳' "
        "AND invoice_no IN (SELECT invoice_no FROM invoices WHERE invoice_type = 'sale' "
        "AND invoice_date >= '2011-06-01')",
    )
    r = run(retail, "gmv", layer, entities=False)
    found = [{(c["dimension"], c["value"]) for c in cause.conditions} for cause in r.causes]
    assert {("country", "Germany")} in found
    assert any(("category", "包袋收纳") in f for f in found)
    assert len(r.rounds) >= 1 and r.rounds[0]["change"] == pytest.approx(-1100)
    no_peel = run(retail, "gmv", layer, entities=False, peel=False)
    assert no_peel.rounds == []


def test_net_sales_and_aov_use_their_formulas(retail, layer):
    alter(
        retail,
        "INSERT INTO invoices VALUES ('C999', 'return', '2011-06-20 10:00:00', 10003, "
        "'United Kingdom', 'returning', 'large', 6000.0)",
        "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, unit_price, "
        "amount) VALUES ('C999', '22222', '包袋收纳', 600, 10.0, 6000.0)",
    )
    net = run(retail, "net_sales", layer)
    assert net.total["change"] == pytest.approx(-6000)
    assert net.causes[0].conditions[0]["value"] == "C999"
    aov = run(retail, "aov", layer)
    assert aov.total["base"] == pytest.approx(300) and aov.causes == []
    assert any("品类" in s or "商品" in s for s in aov.skipped) is False  # 分子能按品类拆


def test_order_count_skips_dimensions_it_cannot_reach(retail, layer):
    alter(retail, "DELETE FROM invoices WHERE country = 'Germany' AND invoice_date >= '2011-06'")
    r = run(retail, "order_count", layer)
    assert r.total["change"] == -4
    assert any(s.startswith("品类") for s in r.skipped)
    assert r.causes[0].conditions[0]["value"] in ("Germany", "Overseas")


def test_every_statement_is_a_single_readonly_select(retail, layer):
    alter(retail, "DELETE FROM invoices WHERE country = 'Germany' AND invoice_date >= '2011-06'")
    r = run(retail, "gmv", layer)
    assert len(r.evidence) >= 8
    for e in r.evidence:
        check_sql(e.sql)  # 不通过会抛 SQLRejected
        assert e.id.startswith("S") and e.columns


def test_scope_filter_restricts_everything(retail, layer):
    alter(
        retail,
        "UPDATE invoice_lines SET amount = 50.0 WHERE category = '包袋收纳' AND invoice_no IN "
        "(SELECT invoice_no FROM invoices WHERE invoice_date >= '2011-06-01')",
    )
    scope = Scope("gmv", MAY, JUN, (("country", ("Germany",)),))
    r = run_attribution(layer, retail, scope)
    assert r.total["base"] == 1200 and r.total["change"] == -200
    assert all(b.dimension != "country" for b in r.breakdowns)


# ---------- 权限 ----------


def test_row_level_policy_applies_to_every_attribution_query(retail, layer):
    alter(
        retail,
        "UPDATE invoice_lines SET amount = 50.0 WHERE category = '包袋收纳' AND invoice_no IN "
        "(SELECT invoice_no FROM invoices WHERE invoice_date >= '2011-06-01')",
    )
    full = run(retail, "gmv", layer)
    limited = run(retail, "gmv", layer, role="overseas_manager")
    assert full.total["base"] == BASE_GMV and limited.total["base"] == 1800  # 只剩德国和法国
    labels = {s.value for b in limited.breakdowns for s in b.segments if b.dimension == "country"}
    assert labels == {"Germany", "France"}
    assert "outside the UK" in limited.role_note
    with pytest.raises(ValueError, match="没有角色"):
        run(retail, "gmv", layer, role="nobody")


# ---------- 数字核对 ----------


def test_extract_numbers_skips_dates_ids_and_ordinals():
    text = (
        "2011 年 1 月退货率 13.65%，比 2010-12 高 11.39 个百分点；"
        "第 1 原因是发票 C541433（客户 10001），前 3 张合计 £77,183.60。"
    )
    got = [(str(v), unit) for _, v, _, unit in extract_numbers(text, {"C541433", "10001"})]
    assert got == [("13.65", "%"), ("11.39", "个百分点"), ("77183.60", "")]


def test_numbers_next_to_chinese_characters_are_read_whole():
    # 模型常常不加空格："从17,547.86英镑涨到91,525.05英镑"；发票号紧跟在汉字后面
    text = "退货额从17,547.86英镑涨到91,525.05英镑，主因是退货单C541433，占84%。"
    got = [str(v) for _, v, _, _ in extract_numbers(text, {"C541433"})]
    assert got == ["17547.86", "91525.05", "84"]


def test_check_numbers_matches_cells_ratios_and_units():
    tables = [[["x", 0.136531, 670362.0], ["y", 91525.3, 12]]]
    ok = check_numbers("退货率 13.65%，退货额 9.15 万，共 12 单，GMV 670,362", tables)
    assert ok.ok and ok.from_sql == 4
    bad = check_numbers("退货率 13.7%，另有 14.2% 来自别处", tables)
    assert bad.unmatched == ["14.2%"] and not bad.ok
    derived = check_numbers("下降 22.5%", tables, derived=[-0.2251])
    assert derived.ok and derived.derived == 1


def test_lenient_mode_accepts_one_step_arithmetic_within_a_table():
    tables = [[["a", 200.0, 150.0]]]
    assert check_numbers("降了 50，降幅 25%", tables).unmatched == ["50", "25%"]
    lenient = check_numbers("降了 50，降幅 25%", tables, lenient=True)
    assert lenient.ok and lenient.lenient == 2


def _with_big_return(retail):
    alter(
        retail,
        "INSERT INTO invoices VALUES ('C999', 'return', '2011-06-20 10:00:00', 10003, "
        "'United Kingdom', 'returning', 'large', 6000.0)",
        "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, unit_price, "
        "amount) VALUES ('C999', '22222', '包袋收纳', 600, 10.0, 6000.0)",
    )


def test_template_answer_always_passes_the_check(retail, layer):
    _with_big_return(retail)
    r = run(retail, "return_rate", layer)
    text = at.template_answer(r)
    assert "C999" in text and "0.92%" in text
    assert at.check_answer(text, r).ok


def test_write_answer_rewrites_then_falls_back_to_template(retail, layer):
    _with_big_return(retail)
    r = run(retail, "return_rate", layer)
    good = "退货率从 0.92% 升到 77.85%，主要是退货单 C999，金额 £6,000.00。"
    bad = "退货率从 0.92% 升到 77.85%，其中 C999 占了 97.3%。"  # 97.3% 哪里都找不到

    async def go(script):
        return await at.write_answer(ScriptedLLM(script), "为什么", r)

    text, check, source, calls = asyncio.run(go([good]))
    assert (source, check.ok, len(calls)) == ("llm", True, 1)
    text, check, source, calls = asyncio.run(go([bad, good]))
    assert (source, text, len(calls)) == ("rewrite", good, 2)
    text, check, source, calls = asyncio.run(go([bad, bad]))
    assert source == "template" and check.ok and "97.3%" not in text


# ---------- 服务接入 ----------


def _plan_json(**kw):
    return plan(metric="return_rate", **kw)


async def _events(svc, question, session="s", role=None):
    return [e async for e in svc.ask(session, "retail_ops", question, role=role)]


def test_service_runs_attribution_for_why_questions_and_caches_it(retail):
    _with_big_return(retail)
    answer = "退货率从 0.92% 升到 77.85%，主要是退货单 C999。"
    llm = ScriptedLLM([_plan_json(), answer])
    svc = ChatService(MemoryStore(), llm_factory=lambda: llm)
    events = asyncio.run(_events(svc, "6 月退货率为什么这么高？"))
    names = [e["event"] for e in events]
    assert names == [
        "step", "metrics", "step", "step", "step", "sql", "attribution", "step", "answer", "done",
    ]  # fmt: skip
    steps = [e["data"]["name"] for e in events if e["event"] == "step"]
    assert steps == ["attr_plan", "attr_total", "attr_decompose", "attr_rank", "attr_answer"]
    attr = next(e["data"] for e in events if e["event"] == "attribution")
    assert attr["causes"][0]["label"] == "单据=C999"
    assert attr["evidence"][0]["sql"].startswith("WITH cell AS")
    ans = next(e["data"] for e in events if e["event"] == "answer")
    assert ans["text"] == answer and ans["number_check"]["unmatched"] == []
    assert ans["source"] == "llm"
    assert {m["name"] for m in events[1]["data"]["items"]} == {"退货率", "退货额", "GMV"}
    # 第二次：命中缓存，不再调用模型（脚本已经用完，再调用会报错）
    again = asyncio.run(_events(svc, "6 月退货率为什么这么高", session="other"))
    assert again[-1]["data"]["cached"] is True
    assert [e["event"] for e in again][:2] == ["metrics", "sql"]


def test_service_cache_is_separate_per_role(retail):
    _with_big_return(retail)
    llm = ScriptedLLM([_plan_json(), "退货率从 0.92% 升到 77.85%。", _plan_json(), "没有数据。"])
    svc = ChatService(MemoryStore(), llm_factory=lambda: llm)
    asyncio.run(_events(svc, "6 月退货率为什么这么高"))
    limited = asyncio.run(_events(svc, "6 月退货率为什么这么高", role="overseas_manager"))
    assert limited[-1]["data"]["cached"] is False
    attr = next(e["data"] for e in limited if e["event"] == "attribution")
    assert attr["total"]["current"] == 0  # 海外经理看不到英国的退货单
    assert "outside the UK" in attr["role_note"]


def test_service_falls_back_to_plain_question_answering(retail):
    llm = ScriptedLLM(
        [
            AttributionPlan(ok=False, reason="复购率没有计算公式"),
            SQLAnswer(sql="SELECT COUNT(*) AS n FROM invoices"),
            "一共 50 张单据。",
        ]
    )
    svc = ChatService(MemoryStore(), llm_factory=lambda: llm)
    events = asyncio.run(_events(svc, "复购率为什么这么低"))
    steps = [e["data"]["name"] for e in events if e["event"] == "step"]
    assert steps[0] == "attr_fallback" and "generate" in steps
    assert "attribution" not in [e["event"] for e in events]
    assert events[-2]["data"]["text"] == "一共 50 张单据。"


def test_shop_database_never_enters_attribution(shop_db_dir):
    llm = ScriptedLLM([SQLAnswer(sql="SELECT COUNT(*) AS n FROM users"), "3 个。"])
    svc = ChatService(MemoryStore(), llm_factory=lambda: llm)

    async def go():
        return [e async for e in svc.ask("s", "shop", "用户数为什么这么少")]

    events = asyncio.run(go())
    assert "attribution" not in [e["event"] for e in events] and events[-2]["event"] == "answer"


# ---------- 构建脚本里的纯函数 ----------


def test_category_rules_first_match_wins_and_size_bands():
    assert categorize("CHRISTMAS LIGHTS") == "节日派对"  # 先匹配先得，不归灯饰
    assert categorize("MEDIUM CERAMIC TOP STORAGE JAR") == "包袋收纳"
    assert categorize("SOMETHING ELSE") == "其他"
    assert [size_band(v) for v in (999.99, 1000, 4999.99, 5000)] == [
        "small", "medium", "medium", "large",
    ]  # fmt: skip


# ---------- 评测：注入、等价说法、判分 ----------


def test_injection_records_equivalents_and_leaves_no_trace(retail, layer):
    from evals.attribution.inject import apply, metric_value

    conn = sqlite3.connect(retail)
    before = metric_value(conn, "gmv", JUN)
    case = {
        "current": list(JUN),
        "seed": 1,
        "ops": [{"op": "delete_invoices", "where": "i.country = 'France'"}],
    }
    inj = apply(conn, case)
    assert metric_value(conn, "gmv", JUN) - before == -600
    eq = inj.equivalents()
    # 法国的单全被删：国家=法国 满足"影响占比和覆盖率都 ≥ 0.8"；海外、英国、品类都不满足
    assert [["country", "France"]] in eq
    assert not any(e[0][0] in ("market", "category", "size_band") for e in eq)
    inj.finish()
    conn.close()
    names = sqlite3.connect(retail).execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {r[0] for r in names} == {
        "countries",
        "customers",
        "invoices",
        "products",
        "invoice_lines",
    }


def test_inserted_returns_look_like_ordinary_invoices(retail):
    from evals.attribution.inject import apply, metric_value

    conn = sqlite3.connect(retail)
    case = {
        "current": list(JUN),
        "seed": 7,
        "ops": [
            {
                "op": "insert_returns",
                "customer_id": 10003,
                "lines": [["22222", 300, 10.0]],
                "days": [5],
            }
        ],
    }
    inj = apply(conn, case)
    new = [r[0] for r in conn.execute("SELECT DISTINCT invoice FROM temp.touch")]
    assert len(new) == 1 and new[0].startswith("C") and new[0][1:].isdigit()
    assert metric_value(conn, "return_rate", JUN) == pytest.approx((72 + 3000) / BASE_GMV)
    eq = inj.equivalents()
    assert [["invoice", new[0]]] in eq and [["size_band", "medium"]] in eq
    conn.close()


def test_scoring_normalizes_values_and_requires_the_exact_grain(layer):
    from evals.attribution_eval import score_causes

    case = {"gold": [[["country", "Germany"]]], "primary": [["country", "Germany"]]}
    hit = score_causes([[["country", "德国"], ["market", "Overseas"]]], case, layer)
    assert hit == {"hit1": True, "hit3": True}  # 中文国名转成库里的写法；国家已决定市场
    coarse = score_causes([[["market", "Overseas"]], [["country", "Germany"]]], case, layer)
    assert coarse == {"hit1": False, "hit3": True}  # 答"海外"太粗，不算第一原因命中
    two = {
        "gold": [[["country", "Germany"]], [["category", "包袋收纳"]]],
        "primary": [["country", "Germany"]],
        "secondary": [["category", "包袋收纳"]],
    }
    got = score_causes([[["category", "包袋收纳"]], [["country", "Germany"]]], two, layer)
    assert got == {"hit1": True, "hit3": True, "both": True}
    assert score_causes([], case, layer) == {"hit1": False, "hit3": False}


# ---------- 对照用的 Agent、示例问题 ----------


def test_agent_runs_sql_then_submits_ranked_causes(retail, layer):
    from datapilot.attr_agent import AttributionAgent, system_prompt

    _with_big_return(retail)
    llm = ScriptedLLM(
        [
            {"name": "run_sql", "input": {"sql": "SELECT COUNT(*) AS n FROM invoices"}},
            {
                "name": "submit_answer",
                "input": {
                    "conclusion": "退货率上升主要来自退货单 C999。",
                    "causes": [{"conditions": [{"dimension": "invoice", "value": "C999"}]}],
                },
            },
        ]
    )
    run = asyncio.run(AttributionAgent()(llm, layer, "6 月退货率为什么这么高"))
    assert run.submitted and run.steps == 2
    assert run.causes == [[["invoice", "C999"]]]
    assert run.queries[0]["rows"] == [[55]]  # 2 × (26 张销售单 + 1 张退货单) + 注入的 1 张
    prompt = system_prompt(layer)
    assert "退货率（公司口径）" in prompt and "invoice: invoices.invoice_no" in prompt
    # Agent 不能写库：和问数同一套只读工具
    bad = ScriptedLLM(
        [
            {"name": "run_sql", "input": {"sql": "DELETE FROM invoices"}},
            {"name": "submit_answer", "input": {"conclusion": "x", "causes": []}},
        ]
    )
    run = asyncio.run(AttributionAgent()(bad, layer, "为什么"))
    assert run.queries[0]["error"] == "rejected"


def test_examples_endpoint_falls_back_to_the_dictionary(retail):
    import httpx

    from datapilot.api import create_app

    app = create_app(ChatService(MemoryStore(), llm_factory=lambda: ScriptedLLM([])))

    async def get(path):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (await c.get(path)).json()

    examples = asyncio.run(get("/api/databases/retail_ops/examples"))
    assert len(examples) == 4 and sum(at.is_why_question(q) for q in examples) == 2
    roles = asyncio.run(get("/api/databases/retail_ops/roles"))
    assert {r["role"] for r in roles} == {"analyst", "overseas_manager"}
