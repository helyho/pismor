import sqlite3

import pytest

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


def test_legacy_v1_db_rejected(tmp_path):
    """v1 库不再迁移：检测到旧版本直接拒绝，要求删库重建。"""
    c = get_conn(tmp_path / "legacy.db")
    c.execute("CREATE TABLE entries (id TEXT PRIMARY KEY)")
    c.execute("PRAGMA user_version=1")
    c.commit()
    with pytest.raises(RuntimeError, match="不兼容"):
        init_schema(c, vec_dimensions=None)
