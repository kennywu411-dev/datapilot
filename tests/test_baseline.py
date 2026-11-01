import asyncio
import sqlite3

import pytest

from datapilot.baseline import SQLAnswer, build_prompt, generate_sql
from datapilot.llm import ScriptedLLM
from datapilot.schema import schema_ddl


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, gender TEXT)")
    conn.execute('CREATE TABLE "order" (id INTEGER, user_id INTEGER REFERENCES users(id))')
    conn.execute("INSERT INTO users VALUES (1, 'F')")
    conn.commit()
    conn.close()
    return path


def test_schema_ddl_lists_all_tables_like_official(db):
    ddl = schema_ddl(db)
    assert "CREATE TABLE users" in ddl
    assert 'CREATE TABLE "order"' in ddl
    assert "sqlite_sequence" not in ddl


def test_prompt_matches_official_format(db):
    p = build_prompt(db, "How many female users?", "female refers to gender = 'F'")
    assert p.startswith("CREATE TABLE users")
    assert "-- Using valid SQLite and understanding External Knowledge, answer the following" in p
    assert "-- How many female users?" in p
    assert "-- External Knowledge: female refers to gender = 'F'" in p


def test_prompt_without_evidence(db):
    p = build_prompt(db, "How many users?", "")
    assert "External Knowledge" not in p


def test_generate_sql_returns_sql_and_cost(db):
    llm = ScriptedLLM([SQLAnswer(sql="SELECT count(*) FROM users WHERE gender = 'F'")])
    out = asyncio.run(generate_sql(llm, db, "q", "e"))
    assert out.sql == "SELECT count(*) FROM users WHERE gender = 'F'"
    assert out.stop_reason == "end_turn"
    assert len(out.llm_calls) == 1


def test_generate_sql_strips_markdown_fence(db):
    llm = ScriptedLLM([SQLAnswer(sql="```sql\nSELECT 1\n```")])
    out = asyncio.run(generate_sql(llm, db, "q", "e"))
    assert out.sql == "SELECT 1"
