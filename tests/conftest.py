import sqlite3

import pytest


@pytest.fixture
def shop_db_dir(tmp_path, monkeypatch):
    """一个带 BIRD 风格列说明的小库：users / order / product。"""
    d = tmp_path / "dbs"
    (d / "shop" / "database_description").mkdir(parents=True)
    conn = sqlite3.connect(d / "shop" / "shop.sqlite")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, gender TEXT, city TEXT)")
    conn.execute(
        'CREATE TABLE "order" (id INTEGER, user_id INTEGER, amount REAL, '
        "FOREIGN KEY (user_id) REFERENCES users(id))"
    )
    conn.execute("CREATE TABLE product (sku TEXT, title TEXT)")
    conn.executemany(
        "INSERT INTO users VALUES (?, ?, ?, ?)",
        [
            (1, "Alice Smith", "F", "New York"),
            (2, "Bob", "M", "Boston"),
            (3, "Carol", "F", "Boston"),
        ],
    )
    conn.executemany('INSERT INTO "order" VALUES (?, ?, ?)', [(1, 1, 9.5), (2, 2, 20.0)])
    conn.execute("INSERT INTO product VALUES ('A1', 'Widget')")
    conn.commit()
    conn.close()
    (d / "shop" / "database_description" / "users.csv").write_text(
        "original_column_name,column_name,column_description,data_format,value_description\n"
        "gender,,user gender,text,F: female; M: male\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("datapilot.config.DB_DIR", d)
    return d
