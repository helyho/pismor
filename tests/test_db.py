import sqlite3

from pismor.core.db import get_conn, init_schema


def test_schema_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"entries", "sources", "review_history", "events_seen", "pending_clarifications", "tombstones"} <= names

def test_duplicate_key_rejected(conn):
    conn.execute("INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,body,summary,created_at,updated_at,last_reinforced_at) VALUES ('w','u','preference','lang','pref_lang',0.9,'active','a','a','t','t','t')")
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,body,summary,created_at,updated_at,last_reinforced_at) VALUES ('w','u','preference','lang','pref_lang',0.9,'active','b','b','t','t','t')")

def test_vec_table_optional(tmp_path):
    c = get_conn(tmp_path / "v.db")
    init_schema(c, vec_dimensions=1024)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert "embeddings" in names


def test_legacy_ids_migrated_to_row_key(tmp_path):
    """旧库（id=dimension_topic@scope）启动时迁移为 dimension_topic；重复启动幂等。"""
    c = get_conn(tmp_path / "legacy.db")
    init_schema(c, vec_dimensions=None)
    c.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        " body,summary,created_at,updated_at,last_reinforced_at)"
        " VALUES ('w','u','preference','lang','preference_lang@global',0.9,'active',"
        " 'b','s','t','t','t')")
    c.commit()
    init_schema(c, vec_dimensions=None)
    assert c.execute("SELECT id FROM entries").fetchone()["id"] == "preference_lang"
    init_schema(c, vec_dimensions=None)
    assert c.execute("SELECT id FROM entries").fetchone()["id"] == "preference_lang"
