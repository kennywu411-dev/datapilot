"""D2 固定流程：Schema 增强、Schema 检索、值核对、执行反馈纠错。"""

import asyncio

from datapilot.baseline import SQLAnswer
from datapilot.config import db_path
from datapilot.llm import ScriptedLLM
from datapilot.pipeline import FixedPipeline, PipelineConfig
from datapilot.schema import enhanced_schema, retrieve_tables
from datapilot.value_check import check_values

# ---------- Schema 增强 ----------


def test_enhanced_schema_has_descriptions_and_examples(shop_db_dir):
    s = enhanced_schema("shop")
    assert "CREATE TABLE users" in s
    assert "F: female; M: male" in s  # BIRD 取值说明
    assert "'Boston'" in s  # 文本列示例值
    assert "REFERENCES users(id)" in s
    assert 'CREATE TABLE "order"' in s  # SQL 关键字表名必须加引号


# ---------- Schema 检索（消融） ----------


def test_retrieve_tables_adds_fk_neighbors(shop_db_dir):
    tables = retrieve_tables("shop", "total order amount", "", k=1)
    assert tables[0] == "order"
    assert "users" in tables  # 沿外键补上关联表
    assert "product" not in tables


# ---------- 值核对 ----------


def test_check_values_flags_missing_literal_with_candidates(shop_db_dir):
    issues = check_values("shop", "SELECT count(*) FROM users WHERE gender = 'female'")
    assert len(issues) == 1
    assert "gender" in issues[0] and "'female'" in issues[0]
    assert "'F'" in issues[0]


def test_check_values_case_and_alias(shop_db_dir):
    sql = "SELECT T1.name FROM users AS T1 WHERE T1.city = 'boston'"
    issues = check_values("shop", sql)
    assert len(issues) == 1 and "'Boston'" in issues[0]


def test_check_values_ok_literal_and_in_list(shop_db_dir):
    assert check_values("shop", "SELECT * FROM users WHERE city IN ('Boston', 'New York')") == []
    assert check_values("shop", "SELECT * FROM users WHERE id = 1") == []
    assert check_values("shop", "SELECT * FROM users WHERE name LIKE 'Al%'") == []


def test_check_values_ignores_unparseable(shop_db_dir):
    assert check_values("shop", "SELEC broken") == []


# ---------- 流程与纠错 ----------


def run(pipe, llm, q="q", e=""):
    return asyncio.run(pipe(llm, db_path("shop"), q, e))


def test_no_issue_single_call(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="enhanced", value_check=True, max_corrections=3))
    llm = ScriptedLLM([SQLAnswer(sql="SELECT count(*) FROM users WHERE gender = 'F'")])
    out = run(pipe, llm)
    assert out.sql.endswith("'F'") and len(out.llm_calls) == 1 and out.first_sql == out.sql


def test_schema_goes_to_cached_system_and_question_to_user(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl"))
    llm = ScriptedLLM([SQLAnswer(sql="SELECT 1")])
    run(pipe, llm, q="How many users?", e="users means rows")
    call = llm.calls[0]
    assert "CREATE TABLE users" in call["system"]
    user = call["messages"][0]["content"]
    assert "How many users?" in user and "CREATE TABLE" not in user


def test_error_triggers_correction_with_feedback(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl", max_corrections=3))
    llm = ScriptedLLM(
        [SQLAnswer(sql="SELECT nope FROM users"), SQLAnswer(sql="SELECT id FROM users")]
    )
    out = run(pipe, llm)
    assert out.sql == "SELECT id FROM users" and out.first_sql == "SELECT nope FROM users"
    feedback = llm.calls[1]["messages"][-1]["content"]
    assert "no such column" in feedback
    # 历史只追加：第二次调用带上第一次的 assistant 回复
    assert llm.calls[1]["messages"][1]["role"] == "assistant"


def test_value_mismatch_triggers_correction(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl", value_check=True, max_corrections=3))
    llm = ScriptedLLM(
        [
            SQLAnswer(sql="SELECT count(*) FROM users WHERE gender = 'female'"),
            SQLAnswer(sql="SELECT count(*) FROM users WHERE gender = 'F'"),
        ]
    )
    out = run(pipe, llm)
    assert out.sql.endswith("'F'")
    assert "'F'" in llm.calls[1]["messages"][-1]["content"]


def test_value_check_disabled_means_no_correction(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl", value_check=False, max_corrections=3))
    llm = ScriptedLLM([SQLAnswer(sql="SELECT count(*) FROM users WHERE gender = 'female'")])
    out = run(pipe, llm)
    # count(*) 返回 [(0,)] 不是空结果，且没开值核对，不纠错
    assert len(out.llm_calls) == 1


def test_empty_result_triggers_correction(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl", max_corrections=1))
    llm = ScriptedLLM(
        [SQLAnswer(sql="SELECT id FROM users WHERE id > 99"), SQLAnswer(sql="SELECT 1")]
    )
    out = run(pipe, llm)
    assert "0 rows" in llm.calls[1]["messages"][-1]["content"]
    assert out.sql == "SELECT 1"


def test_correction_limit_prevents_infinite_loop(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl", max_corrections=2))
    llm = ScriptedLLM([SQLAnswer(sql=f"SELECT bad{i} FROM users") for i in range(5)])
    out = run(pipe, llm)
    assert len(out.llm_calls) == 3  # 1 次生成 + 2 次纠错
    assert len(out.trace) >= 3


def test_correction_disabled_by_default(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl"))
    llm = ScriptedLLM([SQLAnswer(sql="SELECT nope FROM users")])
    out = run(pipe, llm)
    assert len(out.llm_calls) == 1


def test_retrieval_mode_limits_schema(shop_db_dir):
    pipe = FixedPipeline(PipelineConfig(schema="ddl", retrieval_k=1))
    llm = ScriptedLLM([SQLAnswer(sql="SELECT 1")])
    run(pipe, llm, q="total order amount")
    assert "CREATE TABLE product" not in llm.calls[0]["system"]
    assert 'CREATE TABLE "order"' in llm.calls[0]["system"]


def test_correction_without_sql_keeps_previous_sql(shop_db_dir):
    """纠错那次没产出 SQL（如 max_tokens）时，退回上一版而不是交白卷。"""
    from datapilot.llm import LLMResult

    class Flaky(ScriptedLLM):
        async def complete(self, system, messages, tools, output_format=None):
            if len(self.calls) == 1:
                self.calls.append({"system": system, "messages": messages})
                return LLMResult([], "max_tokens", "m")
            return await super().complete(system, messages, tools, output_format)

    pipe = FixedPipeline(PipelineConfig(schema="ddl", max_corrections=3))
    llm = Flaky([SQLAnswer(sql="SELECT id FROM users WHERE id > 99")])
    out = run(pipe, llm)
    assert out.sql == "SELECT id FROM users WHERE id > 99"
    assert len(out.llm_calls) == 2


def test_check_values_alias_scoped_to_subquery(shop_db_dir):
    """外层和子查询都用 T1 指向不同表时，外层的 T1.city 必须解析到 users。"""
    sql = (
        "SELECT T1.name FROM users AS T1 WHERE T1.city = 'boston' "
        'AND T1.id IN (SELECT T1.user_id FROM "order" AS T1 WHERE T1.amount > 1)'
    )
    issues = check_values("shop", sql)
    assert len(issues) == 1 and "users.city" in issues[0] and "'Boston'" in issues[0]


def test_enhanced_schema_examples_use_sql_literals(shop_db_dir):
    import sqlite3

    conn = sqlite3.connect(shop_db_dir / "shop" / "shop.sqlite")
    conn.execute("INSERT INTO product VALUES ('B2', 'O''Brien Gadget')")
    conn.commit()
    conn.close()
    enhanced_schema.cache_clear()
    from datapilot import schema as schema_mod

    schema_mod._examples.cache_clear()
    s = enhanced_schema("shop")
    assert "'O''Brien Gadget'" in s
    assert '"O\'Brien Gadget"' not in s
