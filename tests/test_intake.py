from mem.intake import intake_event

CONV = {"source": "webchat", "user_ref": "u1", "event_id": "e1", "event_type": "conversation",
        "session_id": "s1", "turn": 1, "ts": "2026-11-24T10:00:00+08:00",
        "messages": [{"role": "user", "text": "以后都用 Python"}]}

def test_first_event_processed(conn):
    ok, ack = intake_event(conn, CONV)
    assert ok and not ack.get("deduplicated")
    assert conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"] == 1

def test_duplicate_event_deduplicated(conn):  # Review Focus #1
    intake_event(conn, CONV)
    ok, ack = intake_event(conn, CONV)
    assert not ok and ack == {"deduplicated": True}

def test_hash_fallback_event_id(conn):
    ev = dict(CONV, event_id=None)
    ok, _ = intake_event(conn, ev)
    assert ok
    row = conn.execute("SELECT event_id FROM events_seen").fetchone()
    assert row["event_id"] and row["event_id"].startswith("sha256:")

def test_unknown_event_type_rejected(conn):
    import pytest
    with pytest.raises(Exception):
        intake_event(conn, dict(CONV, event_id="e2", event_type="audio"))

def test_text_event_accepted(conn):
    ev = {"source": "docs", "user_ref": "u1", "event_id": "e9", "event_type": "text",
          "ts": "2026-11-24T09:00:00+08:00", "content": "用户本周笔记……"}
    ok, _ = intake_event(conn, ev)
    assert ok
