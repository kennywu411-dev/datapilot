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
    # 内容一致（backup API 拷贝，字节可能不同）
    dst = sqlite3.connect(out)
    assert (
        dst.execute("SELECT sql FROM sqlite_master WHERE name = 'users'")
        .fetchone()[0]
        .startswith("CREATE TABLE users")
    )
    dst.close()

    rows = list(csv.DictReader((tmp_path / "dbs/shop2/database_description/users.csv").open()))
    tier = next(r for r in rows if r["original_column_name"] == "tier")
    assert tier["value_description"] == "normal / vip"


def test_add_database_includes_uncheckpointed_wal_rows(tmp_path, monkeypatch):
    """源库在 WAL 模式、改动还在 -wal 文件里时，也要拷到最新数据。"""
    src = tmp_path / "wal.db"
    writer = sqlite3.connect(src)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (a)")
    writer.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    writer.commit()  # 不关闭、不 checkpoint：数据留在 -wal 文件里
    monkeypatch.setattr("datapilot.config.DB_DIR", tmp_path / "dbs")
    out = add_database("w", src)
    assert sqlite3.connect(out).execute("SELECT count(*) FROM t").fetchone() == (5,)
    writer.close()


def test_add_database_refuses_to_overwrite(tmp_path, monkeypatch):
    import pytest

    src = tmp_path / "src.db"
    sqlite3.connect(src).execute("CREATE TABLE t (a)").connection.commit()
    monkeypatch.setattr("datapilot.config.DB_DIR", tmp_path / "dbs")
    add_database("x", src)
    with pytest.raises(FileExistsError):
        add_database("x", src)
