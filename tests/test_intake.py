import pytest
from pydantic import ValidationError

from pismor.pipeline.intake import intake_event

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
    with pytest.raises(ValidationError):
        intake_event(conn, dict(CONV, event_id="e2", event_type="audio"))

def test_text_over_8192_rejected(conn):  # Review Important #1：8K 硬顶，拒绝且不落库
    ev = {"source": "docs", "user_ref": "u1", "event_id": "e10", "event_type": "text",
          "ts": "2026-11-24T09:00:00+08:00", "content": "x" * 8193}
    with pytest.raises(ValidationError):
        intake_event(conn, ev)
    assert conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


def test_max_content_chars_configurable(conn):
    ev = {"source": "docs", "user_ref": "u1", "event_id": "e12", "event_type": "text",
          "ts": "2026-11-24T09:00:00+08:00", "content": "x" * 50}
    with pytest.raises(ValidationError):  # 上限收紧到 10：50 字符被拒且不落库
        intake_event(conn, dict(ev), max_content_chars=10)
    assert conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"] == 0
    ok, _ = intake_event(conn, dict(ev), max_content_chars=100)  # 放宽到 100：放行
    assert ok

@pytest.mark.parametrize("field", ["source", "user_ref", "session_id", "ts"])
def test_missing_required_field_rejected(conn, field):  # Review Important #1：缺必填字段拒绝且不落库
    ev = {k: v for k, v in CONV.items() if k != field} | {"event_id": "e11"}
    with pytest.raises(ValidationError):
        intake_event(conn, ev)
    assert conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"] == 0

def test_text_event_accepted(conn):
    ev = {"source": "docs", "user_ref": "u1", "event_id": "e9", "event_type": "text",
          "ts": "2026-11-24T09:00:00+08:00", "content": "用户本周笔记……"}
    ok, _ = intake_event(conn, ev)
    assert ok


# ---- fix wave 新增：入口校验 / hash 兜底回写 ----

def test_empty_messages_rejected(conn):
    with pytest.raises(ValidationError):
        intake_event(conn, dict(CONV, event_id="e20", messages=[]))
    assert conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"] == 0


@pytest.mark.parametrize("message", [{"role": "user"}, {"role": "user", "text": " "},
                                     {"role": "system", "text": "x"}])
def test_invalid_message_rejected(conn, message):
    with pytest.raises(ValidationError):
        intake_event(conn, dict(CONV, event_id="e21", messages=[message]))
    assert conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"] == 0


@pytest.mark.parametrize("ts", ["not-a-time", "2026-11-24T10:00:00"])
def test_invalid_ts_rejected(conn, ts):
    # 无时区/非 ISO 的 ts 会让 julianday 归 NULL，降级/归档静默失效
    with pytest.raises(ValidationError):
        intake_event(conn, dict(CONV, event_id="e22", ts=ts))
    assert conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"] == 0


def test_hash_fallback_writes_back_event_id(conn):
    ev = dict(CONV, event_id=None)
    ok, ack = intake_event(conn, ev)
    assert ok and ev["event_id"] == ack["event_id"] and ev["event_id"].startswith("sha256:")
