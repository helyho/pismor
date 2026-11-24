import sqlite3

from mem.db import get_conn, init_schema


def test_schema_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"entries", "sources", "review_history", "events_seen", "pending_clarifications", "tombstones"} <= names

def test_cross_scope_same_topic_allowed(conn):
    # Review Focus #3: 跨作用域同 topic 共存
    conn.execute("INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,created_at,updated_at,last_reinforced_at) VALUES ('webchat','u1','preference','lang','global','pref_lang',0.9,'active','主语言 Python','主语言Python','2026-01-01','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,created_at,updated_at,last_reinforced_at) VALUES ('webchat','u1','preference','lang','project:x','pref_lang',0.9,'active','项目内用 Go','项目用Go','2026-01-01','2026-01-01','2026-01-01')")
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 2

def test_duplicate_key_rejected(conn):
    conn.execute("INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,created_at,updated_at,last_reinforced_at) VALUES ('w','u','preference','lang','global','pref_lang',0.9,'active','a','a','t','t','t')")
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,created_at,updated_at,last_reinforced_at) VALUES ('w','u','preference','lang','global','pref_lang',0.9,'active','b','b','t','t','t')")

def test_vec_table_optional(tmp_path):
    c = get_conn(tmp_path / "v.db")
    init_schema(c, vec_dimensions=1024)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert "embeddings" in names
