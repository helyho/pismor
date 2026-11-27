import json
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.core.db import get_conn
from pismor.serving.api import create_app
from pismor.serving.review import apply_review_decision


REPO = Path(__file__).resolve().parents[2]
VERSION = "2026-01-01T00:00:00+00:00"


class FakeLLM:
    def chat_json(self, system, user, schema):
        return {"candidates": []}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "delete.db",
        taxonomy_path=REPO / "config" / "taxonomy.yaml",
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(
            enabled=False, endpoint="", model="", api_key_env="",
            dimensions=4, batch_size=8,
        ),
    )


def _make_app(tmp_path: Path):
    app = create_app(_settings(tmp_path), llm=FakeLLM(), embedder=None)
    return app, TestClient(app), tmp_path / "delete.db"


def _insert(
    conn,
    entry_id,
    *,
    user_source="webchat",
    user_id="u1",
    dimension="preference",
    topic="style",
    variant="normal",
    variant_seq=0,
    status="active",
    summary="summary",
    body="body",
    conflict_with=None,
    superseded_by=None,
    updated_at=VERSION,
):
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,variant,variant_seq,"
        "confidence,status,body,summary,created_at,updated_at,last_reinforced_at,"
        "conflict_with,superseded_by)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_source, user_id, dimension, topic, entry_id, variant, variant_seq,
         0.9, status, body, summary, updated_at, updated_at, updated_at,
         conflict_with, superseded_by),
    )


def _seed(conn):
    _insert(conn, "preference_style", conflict_with='["conflict_style"]')
    _insert(conn, "conflict_style", variant="conflict", variant_seq=1,
            conflict_with='["preference_style"]')
    _insert(conn, "conflict_style_other_dim", dimension="fact", topic="style",
            variant="conflict", variant_seq=1)
    _insert(conn, "fact_tone", dimension="fact", topic="tone",
            conflict_with='["preference_style"]', superseded_by="preference_style")
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        ("preference_style", "webchat", "u1", "text", "s1", "原话", "explicit"),
    )
    conn.execute(
        "INSERT INTO review_history (entry_id,user_source,user_id,decision,actor,ts,"
        "note,entry_version) VALUES (?,?,?,?,?,?,?,?)",
        ("preference_style", "webchat", "u1", "flagged", "audit", VERSION, None, VERSION),
    )
    conn.execute(
        "INSERT INTO pending_clarifications (user_source,user_id,conflicting_ids,"
        "question,issued_at,ttl_days) VALUES (?,?,?,?,?,?)",
        ("webchat", "u1", '["preference_style","fact_tone"]', "哪条对？", VERSION, 7),
    )
    conn.commit()


def test_preview_lists_cascade_by_dimension_and_topic(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _seed(conn)

    res = client.get("/admin/api/entries/delete-preview", params={
        "user_source": "webchat", "user_id": "u1", "entry_id": "preference_style",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["target"]["id"] == "preference_style"
    assert [row["id"] for row in body["cascaded"]] == ["conflict_style"]
    assert body["total"] == 2
    assert body["warnings"] == []


def test_preview_warns_taboo(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert(conn, "taboo_politics", dimension="taboo", topic="politics")
        conn.commit()

    res = client.get("/admin/api/entries/delete-preview", params={
        "user_source": "webchat", "user_id": "u1", "entry_id": "taboo_politics",
    })
    assert res.status_code == 200
    assert res.json()["warnings"]


def test_delete_cascades_and_cleans_references(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _seed(conn)

    res = client.delete("/admin/api/entries", params={
        "user_source": "webchat", "user_id": "u1", "entry_id": "preference_style",
        "expected_version": VERSION,
    })
    assert res.status_code == 200
    assert sorted(res.json()["deleted"]) == ["conflict_style", "preference_style"]

    with closing(get_conn(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id IN ('preference_style','conflict_style')"
        ).fetchone()[0] == 0
        # 同 topic 不同维度的 conflict 不受影响
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id='conflict_style_other_dim'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sources WHERE entry_id='preference_style'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM review_history WHERE entry_id='preference_style'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_clarifications WHERE user_id='u1'"
        ).fetchone()[0] == 0
        other = conn.execute(
            "SELECT conflict_with, superseded_by FROM entries WHERE id='fact_tone'"
        ).fetchone()
        assert other["conflict_with"] is None
        assert other["superseded_by"] is None
        audit = conn.execute(
            "SELECT actor, action, detail FROM admin_audit WHERE action='entry.delete'"
        ).fetchone()
        assert audit["actor"] == "local-admin"
        assert "conflict_style" in audit["detail"]


def test_delete_without_cascade_keeps_conflict(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _seed(conn)

    res = client.delete("/admin/api/entries", params={
        "user_source": "webchat", "user_id": "u1", "entry_id": "preference_style",
        "cascade": "false",
    })
    assert res.status_code == 200
    assert res.json()["deleted"] == ["preference_style"]
    with closing(get_conn(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id='conflict_style'"
        ).fetchone()[0] == 1


def test_delete_version_mismatch_is_409(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _seed(conn)

    res = client.delete("/admin/api/entries", params={
        "user_source": "webchat", "user_id": "u1", "entry_id": "preference_style",
        "expected_version": "old",
    })
    assert res.status_code == 409
    with closing(get_conn(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id='preference_style'"
        ).fetchone()[0] == 1


def test_public_delete_idempotent_and_tombstone(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _seed(conn)

    res = client.delete("/memory/webchat/u1/entries/preference_style",
                        headers={"x-requested-by": "app-42"})
    assert res.status_code == 200
    assert sorted(res.json()["deleted"]) == ["conflict_style", "preference_style"]

    res = client.delete("/memory/webchat/u1/entries/preference_style")
    assert res.status_code == 204

    with closing(get_conn(db_path)) as conn:
        audit = conn.execute(
            "SELECT actor, action FROM admin_audit WHERE action='entry.delete.public'"
        ).fetchone()
        assert audit["actor"] == "app-42"
        conn.execute(
            "INSERT INTO tombstones (user_source,user_id,deleted_at,requested_by)"
            " VALUES ('webchat','u1',?,'api')", (VERSION,))
        conn.commit()

    res = client.delete("/memory/webchat/u1/entries/fact_tone")
    assert res.status_code == 410


def test_review_remove_does_not_cascade(tmp_path):
    _app, _client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _seed(conn)
        with conn:
            result = apply_review_decision(
                conn,
                user_source="webchat",
                user_id="u1",
                entry_id="preference_style",
                decision="remove",
                entry_version=VERSION,
            )
        assert result["noop"] is False
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id='conflict_style'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM entries WHERE id='preference_style'"
        ).fetchone()[0] == 0


def test_delete_trace_and_snapshot(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert(conn, "trace_style", variant="trace", variant_seq=1, status="archived")
        _insert(conn, "snapshot_tone", dimension="fact", topic="tone",
                variant="snapshot", variant_seq=1, status="pending_review")
        conn.commit()

    for entry_id in ("trace_style", "snapshot_tone"):
        res = client.delete("/admin/api/entries", params={
            "user_source": "webchat", "user_id": "u1", "entry_id": entry_id,
        })
        assert res.status_code == 200
        assert res.json()["deleted"] == [entry_id]


def test_delete_taboo_refreshes_merge_task(tmp_path):
    _app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert(conn, "taboo_politics", dimension="taboo", topic="politics")
        conn.execute(
            "INSERT INTO merge_tasks (user_source,user_id,taboo_count,status,"
            "proposal_json,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("webchat", "u1", 1, "open", json.dumps({"topics": ["politics"]}),
             None, VERSION, VERSION),
        )
        conn.commit()

    res = client.delete("/admin/api/entries", params={
        "user_source": "webchat", "user_id": "u1", "entry_id": "taboo_politics",
    })
    assert res.status_code == 200
    with closing(get_conn(db_path)) as conn:
        task = conn.execute(
            "SELECT taboo_count, status, proposal_json FROM merge_tasks"
        ).fetchone()
        assert task["taboo_count"] == 0
        assert task["status"] == "dismissed"
        assert task["proposal_json"] is None
