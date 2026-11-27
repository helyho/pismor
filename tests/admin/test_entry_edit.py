import json
import struct
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from pismor.admin.deps import Principal
from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.core.db import get_conn, init_schema
from pismor.serving.api import create_app


REPO = Path(__file__).resolve().parents[2]
VERSION = "2026-01-01T00:00:00+00:00"


class FakeLLM:
    def chat_json(self, system, user, schema):
        return {"candidates": []}


class FakeEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _settings(tmp_path: Path, *, embedding_enabled: bool = False) -> Settings:
    return Settings(
        db_path=tmp_path / "admin.db",
        taxonomy_path=REPO / "config" / "taxonomy.yaml",
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(
            enabled=embedding_enabled, endpoint="", model="", api_key_env="",
            dimensions=4, batch_size=8,
        ),
    )


def _make_app(tmp_path: Path, *, embedder=None, embedding_enabled: bool = False):
    app = create_app(
        _settings(tmp_path, embedding_enabled=embedding_enabled),
        llm=FakeLLM(), embedder=embedder,
    )
    return app, TestClient(app), tmp_path / "admin.db"


def _insert_entry(
    conn,
    *,
    user_source="webchat",
    user_id="u1",
    dimension="preference",
    topic="style",
    status="active",
    summary="summary",
    body="body",
    conflict_with=None,
    updated_at=VERSION,
):
    entry_id = f"{dimension}_{topic}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,"
        "status,body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,"
        "superseded_by,topic_new,conflict_with,reinforcements,archived_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_source, user_id, dimension, topic, entry_id, 0.9, status,
         body, summary, updated_at, updated_at, updated_at, None, None, None,
         conflict_with, 0, None),
    )
    conn.commit()
    return entry_id


def _payload(entry_id="preference_style", **overrides):
    payload = {
        "user_source": "webchat",
        "user_id": "u1",
        "entry_id": entry_id,
        "expected_version": VERSION,
        "new_dimension": "fact",
        "new_topic": "tone",
        "confidence": 0.8,
        "status": "active",
        "summary": "new summary",
        "body": "new body",
        "degraded_reason": None,
    }
    payload.update(overrides)
    return payload


def test_edit_check_conflict_and_validation(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert_entry(conn, dimension="preference", topic="style")
        _insert_entry(conn, dimension="fact", topic="tone")

    res = client.post("/admin/api/entries/validate-edit", json=_payload())
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["conflicts"][0]["id"] == "fact_tone"

    res = client.patch("/admin/api/entries", json=_payload())
    assert res.status_code == 409
    assert res.json()["detail"]["conflicts"][0]["id"] == "fact_tone"

    res = client.patch("/admin/api/entries", json=_payload(new_dimension="missing"))
    assert res.status_code == 422

    app.state.principal = Principal(
        subject="nobody", display_name="无权限", roles=(), capabilities=frozenset()
    )
    res = client.post("/admin/api/entries/validate-edit", json=_payload())
    assert res.status_code == 403


def test_edit_renames_entry_and_syncs_references(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        init_schema(conn, 4)  # 测试启用 sqlite-vec，不依赖真实 embedding 服务
        entry_id = _insert_entry(conn, dimension="preference", topic="style")
        _insert_entry(
            conn, dimension="preference", topic="other",
            conflict_with=json.dumps([entry_id], ensure_ascii=False),
        )
        conn.execute(
            "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
            " VALUES (?,?,?,?,?,?,?)",
            (entry_id, "webchat", "u1", "message", "m1", "原话", "explicit"),
        )
        conn.execute(
            "INSERT INTO review_history (entry_id,user_source,user_id,decision,actor,ts,note,entry_version)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (entry_id, "webchat", "u1", "confirm", "reviewer", VERSION, None, VERSION),
        )
        conn.execute(
            "INSERT INTO pending_clarifications (user_source,user_id,conflicting_ids,question,issued_at,ttl_days)"
            " VALUES (?,?,?,?,?,?)",
            ("webchat", "u1", json.dumps([entry_id], ensure_ascii=False), "哪个更准确？", VERSION, 7),
        )
        old_vec = entry_id
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (old_vec, "webchat", "u1", json.dumps([0.1, 0.2, 0.3, 0.4])),
        )
        conn.commit()

    res = client.patch("/admin/api/entries", json=_payload(entry_id, body="body"))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["id"] == entry_id

    with closing(get_conn(db_path)) as conn:
        entry = conn.execute(
            "SELECT * FROM entries WHERE user_source='webchat' AND user_id='u1' AND id=?",
            (entry_id,),
        ).fetchone()
        assert entry["dimension"] == "fact"
        assert entry["topic"] == "tone"
        assert entry["summary"] == "new summary"
        assert entry["body"] == "body"
        assert conn.execute(
            "SELECT entry_id FROM sources WHERE origin_ref='m1'"
        ).fetchone()["entry_id"] == entry_id
        assert conn.execute(
            "SELECT entry_id FROM review_history WHERE entry_id=?",
            (entry_id,),
        ).fetchone() is not None
        other = conn.execute(
            "SELECT conflict_with FROM entries WHERE id='preference_other'"
        ).fetchone()
        assert json.loads(other["conflict_with"]) == [entry_id]
        pending = conn.execute(
            "SELECT conflicting_ids FROM pending_clarifications WHERE user_source='webchat' AND user_id='u1'"
        ).fetchone()
        assert json.loads(pending["conflicting_ids"]) == [entry_id]
        assert conn.execute(
            "SELECT 1 FROM embeddings WHERE entry_id=?", (entry_id,)
        ).fetchone() is not None
        audit = conn.execute(
            "SELECT actor, action, target FROM admin_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert (audit["actor"], audit["action"], audit["target"]) == (
            "local-admin", "entry.edit", "fact_tone",
        )


def test_edit_stale_version_and_body_invalidates_vector(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        init_schema(conn, 4)
        entry_id = _insert_entry(conn, topic="style")
        old_vec = entry_id
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (old_vec, "webchat", "u1", json.dumps([0.1, 0.2, 0.3, 0.4])),
        )
        conn.commit()

    res = client.patch(
        "/admin/api/entries",
        json=_payload(entry_id, expected_version="2025-01-01T00:00:00+00:00"),
    )
    assert res.status_code == 409
    assert res.json()["detail"]["errors"]

    res = client.patch(
        "/admin/api/entries",
        json=_payload(
            entry_id,
            new_dimension="preference",
            new_topic="style",
            body="changed body",
        ),
    )
    assert res.status_code == 200
    assert any("嵌入未启用" in w for w in res.json()["warnings"])
    with closing(get_conn(db_path)) as conn:
        assert conn.execute(
            "SELECT 1 FROM embeddings WHERE entry_id=?", (old_vec,)
        ).fetchone() is None


def test_edit_body_reembeds_vector_with_new_text(tmp_path):
    app, client, db_path = _make_app(
        tmp_path, embedder=FakeEmbedder(), embedding_enabled=True)
    with closing(get_conn(db_path)) as conn:
        entry_id = _insert_entry(conn, topic="style")
        old_vec = entry_id
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (old_vec, "webchat", "u1", json.dumps([0.9, 0.9, 0.9, 0.9])),
        )
        conn.commit()
    res = client.patch(
        "/admin/api/entries",
        json=_payload(
            entry_id,
            new_dimension="preference",
            new_topic="style",
            body="changed body",
        ),
    )
    assert res.status_code == 200, res.text
    assert any("向量已重建" in w for w in res.json()["warnings"])
    with closing(get_conn(db_path)) as conn:
        row = conn.execute(
            "SELECT embedding FROM embeddings WHERE entry_id=?", (old_vec,)
        ).fetchone()
    assert row is not None
    # 旧向量已换成新正文的归一化嵌入，而不是保留旧的 [0.9, 0.9, 0.9, 0.9]
    raw = struct.unpack("<4f", row["embedding"])
    expected = [0.1, 0.2, 0.3, 0.4]
    norm = sum(v * v for v in expected) ** 0.5
    assert all(abs(a - b / norm) < 1e-6 for a, b in zip(raw, expected))
