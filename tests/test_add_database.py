"""接入新库：复制数据库、从建表语句的行尾注释生成列说明草稿。"""

import csv
import sqlite3

from scripts.add_database import add_database


def test_add_database_copies_and_drafts_descriptions(tmp_path, monkeypatch):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute(
        "CREATE TABLE users (\n"
        "  user_id TEXT PRIMARY KEY,\n"
        "  tier TEXT NOT NULL  -- normal / vip\n"
        ")"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("datapilot.config.DB_DIR", tmp_path / "dbs")

    out = add_database("shop2", src)
    assert out == tmp_path / "dbs" / "shop2" / "shop2.sqlite" and out.exists()
    assert src.read_bytes() == out.read_bytes()  # 原样复制，不改源库

    rows = list(csv.DictReader((tmp_path / "dbs/shop2/database_description/users.csv").open()))
    tier = next(r for r in rows if r["original_column_name"] == "tier")
    assert tier["value_description"] == "normal / vip"


def test_add_database_refuses_to_overwrite(tmp_path, monkeypatch):
    import pytest

    src = tmp_path / "src.db"
    sqlite3.connect(src).execute("CREATE TABLE t (a)").connection.commit()
    monkeypatch.setattr("datapilot.config.DB_DIR", tmp_path / "dbs")
    add_database("x", src)
    with pytest.raises(FileExistsError):
        add_database("x", src)
