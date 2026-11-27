import json
import shutil
import struct
from contextlib import closing
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from pismor.admin.deps import Principal
from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.core.db import get_conn, init_schema
from pismor.core.taxonomy import load_taxonomy
from pismor.serving.api import create_app


REPO = Path(__file__).resolve().parents[2]
VERSION = "2026-01-01T00:00:00+00:00"


class FakeLLM:
    def chat_json(self, system, user, schema):
        return {"candidates": []}


class FakeEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _settings(tmp_path: Path, *, embedding_enabled: bool = False,
              taxonomy_path: Path | None = None) -> Settings:
    return Settings(
        db_path=tmp_path / "admin.db",
        taxonomy_path=taxonomy_path or REPO / "config" / "taxonomy.yaml",
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(
            enabled=embedding_enabled, endpoint="", model="", api_key_env="",
            dimensions=4, batch_size=8,
        ),
    )


def _make_app(tmp_path: Path, *, embedder=None, embedding_enabled: bool = False,
              taxonomy_path: Path | None = None):
    app = create_app(
        _settings(tmp_path, embedding_enabled=embedding_enabled,
                  taxonomy_path=taxonomy_path),
        llm=FakeLLM(), embedder=embedder)
    return app, TestClient(app), tmp_path / "admin.db"


def _insert_entry(
    conn,
    *,
    user_source="webchat",
    user_id="u1",
    dimension="preference",
    topic="style",
    status="pending_review",
    conflict_with=None,
    summary="summary",
    body="body",
    updated_at=VERSION,
    variant="normal",
    variant_seq=0,
):
    entry_id = (f"{dimension}_{topic}" if variant == "normal"
                else f"{dimension}_{topic}_{variant}")
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,"
        "status,body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,"
        "superseded_by,topic_new,conflict_with,reinforcements,archived_at,"
        "variant,variant_seq)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_source, user_id, dimension, topic, entry_id, 0.9, status,
         body, summary, updated_at, updated_at, updated_at, None, None, None,
         conflict_with, 0, None, variant, variant_seq),
    )
    conn.commit()
    return entry_id


def test_queue_filters_and_pagination(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert_entry(conn, topic="a", dimension="preference", summary="alpha",
                      updated_at="2026-01-01T00:00:00+00:00")
        _insert_entry(conn, topic="b", dimension="fact", user_id="u2", summary="beta",
                      updated_at="2026-01-02T00:00:00+00:00")
        _insert_entry(conn, topic="conflict", dimension="preference", status="active",
                      summary="conflict summary", conflict_with='["preference_a"]',
                      updated_at="2026-01-03T00:00:00+00:00",
                      variant="conflict", variant_seq=1)
        _insert_entry(conn, topic="old", dimension="fact", status="archived",
                      summary="archived", updated_at="2026-01-04T00:00:00+00:00")

    res = client.get("/admin/api/review/queue", params={"page": 1, "page_size": 1})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert body["page_size"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["topic"] == "a"
    assert body["items"][0]["variant"] == "normal"

    res = client.get("/admin/api/review/queue", params={"dimension": "preference"})
    assert res.json()["total"] == 2

    res = client.get("/admin/api/review/queue", params={"q": "conflict"})
    assert res.json()["total"] == 1
    assert res.json()["items"][0]["topic"] == "conflict"

    res = client.get("/admin/api/review/queue", params={"user_id": "u2"})
    assert res.json()["total"] == 1
    assert res.json()["items"][0]["user_id"] == "u2"

    res = client.get("/admin/api/review/facets")
    facets = res.json()
    assert {item["value"] for item in facets["dimensions"]} == {"preference", "fact"}
    assert facets["sources"] == [{"value": "webchat", "count": 3}]


def test_entry_detail_returns_evidence_history_and_conflict(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        entry_id = _insert_entry(
            conn, topic="style", conflict_with='["preference_other"]')
        _insert_entry(conn, topic="other", status="active", summary="other summary")
        conn.execute(
            "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
            " VALUES (?,?,?,?,?,?,?)",
            (entry_id, "webchat", "u1", "message", "msg-1", "我喜欢简洁", "explicit"),
        )
        conn.execute(
            "INSERT INTO review_history (entry_id,user_source,user_id,decision,actor,ts,note,entry_version)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (entry_id, "webchat", "u1", "confirm", "reviewer", VERSION, "ok", VERSION),
        )
        conn.execute(
            "INSERT INTO pending_clarifications (user_source,user_id,conflicting_ids,question,issued_at,ttl_days)"
            " VALUES (?,?,?,?,?,?)",
            ("webchat", "u1", json.dumps([entry_id]), "哪个更准确？", VERSION, 7),
        )
        conn.commit()

    res = client.get(
        "/admin/api/review/entry",
        params={
            "user_source": "webchat",
            "user_id": "u1",
            "entry_id": entry_id,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["entry"]["conflict_marks"] == ["preference_other"]
    assert body["entry"]["variant"] == "normal"
    assert body["entry"]["variant_seq"] == 0
    assert body["sources"][0]["quote"] == "我喜欢简洁"
    assert body["history"][0]["decision"] == "confirm"
    assert body["history"][0]["actor"] == "reviewer"
    assert body["clarifications"][0]["conflicting_ids"] == [entry_id]
    assert body["conflicts"][0]["summary"] == "other summary"


def test_confirm_remove_fix_and_noop(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        entry_id = _insert_entry(conn, topic="style")

    payload = {
        "user_source": "webchat",
        "user_id": "u1",
        "entry_id": entry_id,
        "decision": "confirm",
        "entry_version": VERSION,
    }
    res = client.post("/admin/api/review/decision", json=payload)
    assert res.status_code == 200
    assert res.json() == {"noop": False, "updated": 1}
    with closing(get_conn(db_path)) as conn:
        entry = conn.execute("SELECT status FROM entries WHERE id=?", (entry_id,)).fetchone()
        history = conn.execute(
            "SELECT decision, actor FROM review_history WHERE entry_id=?", (entry_id,)
        ).fetchone()
    assert entry["status"] == "active"
    assert (history["decision"], history["actor"]) == ("confirm", "reviewer")

    # 同一版本再次判定：版本已推进，返回 noop
    res = client.post("/admin/api/review/decision", json=payload)
    assert res.json()["noop"] is True

    # fix：覆盖摘要与正文
    with closing(get_conn(db_path)) as conn:
        current = conn.execute(
            "SELECT updated_at FROM entries WHERE id=?", (entry_id,)
        ).fetchone()["updated_at"]
    res = client.post("/admin/api/review/decision", json={
        **payload, "decision": "fix", "entry_version": current,
        "summary": "fixed summary", "body": "fixed body",
    })
    assert res.json()["noop"] is False
    with closing(get_conn(db_path)) as conn:
        entry = conn.execute(
            "SELECT summary, body FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
    assert (entry["summary"], entry["body"]) == ("fixed summary", "fixed body")

    # remove：条目、证据链与其处理历史一并物理删除
    with closing(get_conn(db_path)) as conn:
        current = conn.execute(
            "SELECT updated_at FROM entries WHERE id=?", (entry_id,)
        ).fetchone()["updated_at"]
        conn.execute(
            "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
            " VALUES (?,?,?,?,?,?,?)",
            (entry_id, "webchat", "u1", "message", "msg-2", "原文", "explicit"),
        )
        conn.commit()
    res = client.post("/admin/api/review/decision", json={
        **payload, "decision": "remove", "entry_version": current,
    })
    assert res.json()["noop"] is False
    with closing(get_conn(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        decisions = [row[0] for row in conn.execute(
            "SELECT decision FROM review_history ORDER BY rowid")]
    assert decisions == []


def test_detail_404_and_capability(tmp_path):
    app, client, _ = _make_app(tmp_path)
    res = client.get("/admin/api/review/entry", params={
        "user_source": "webchat", "user_id": "u1",
        "entry_id": "preference_missing",
    })
    assert res.status_code == 404

    app.state.principal = Principal(
        subject="nobody", display_name="无权限", roles=(), capabilities=frozenset()
    )
    assert client.get("/admin/api/review/queue").status_code == 403
    assert client.post("/admin/api/review/decision", json={}).status_code == 403


def _add_source(conn, entry_id, quote="不要表格"):
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (entry_id, "webchat", "u1", "message", "m1", quote, "explicit"))
    conn.commit()


def _decide(client, entry_id, topic):
    return client.post("/admin/api/review/decision", json={
        "user_source": "webchat", "user_id": "u1",
        "entry_id": entry_id, "decision": "confirm",
        "entry_version": VERSION, "topic": topic})


def test_confirm_rekeys_free_topic_to_whitelist(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        eid = _insert_entry(conn, dimension="taboo", topic="不要表格", body="不要用表格")
        _add_source(conn, eid)
    res = _decide(client, eid, "output_format")
    assert res.status_code == 200, res.text
    assert res.json()["rekeyed_to"] == "output_format"
    with closing(get_conn(db_path)) as conn:
        row = conn.execute("SELECT topic,id,status FROM entries").fetchone()
        assert row["topic"] == "output_format"
        assert row["id"] == eid
        assert row["status"] == "active"
        assert conn.execute("SELECT entry_id FROM sources").fetchone()["entry_id"] == row["id"]


def test_confirm_merges_into_existing_topic(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert_entry(conn, dimension="taboo", topic="output_format",
                      body="规范条目", status="active")
        eid = _insert_entry(conn, dimension="taboo", topic="不要表格", body="自由说法")
        _add_source(conn, eid)
    res = _decide(client, eid, "output_format")
    assert res.status_code == 200, res.text
    assert res.json()["merged_into"] == "output_format"
    with closing(get_conn(db_path)) as conn:
        rows = conn.execute("SELECT id,body FROM entries").fetchall()
        assert [r["id"] for r in rows] == ["taboo_output_format"]
        assert rows[0]["body"] == "规范条目"  # 保留规范条目正文
        assert conn.execute("SELECT entry_id FROM sources").fetchone()["entry_id"] == \
            "taboo_output_format"


def test_confirm_rejects_topic_outside_whitelist(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        eid = _insert_entry(conn, dimension="taboo", topic="不要表格", body="不要用表格")
    res = _decide(client, eid, "not_in_whitelist")
    assert res.status_code == 200, res.text
    assert res.json()["invalid_topic"] is True
    with closing(get_conn(db_path)) as conn:
        assert conn.execute("SELECT topic FROM entries").fetchone()["topic"] == "不要表格"


def test_merge_tasks_list_and_resolve(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        conn.execute(
            "INSERT INTO merge_tasks (user_source,user_id,taboo_count,status,proposal_json,"
            "created_at,updated_at) VALUES ('webchat','u1',50,'open','{}',?,?)",
            (VERSION, VERSION))
        conn.commit()
        tid = conn.execute("SELECT id FROM merge_tasks").fetchone()["id"]
    res = client.get("/admin/api/merge-tasks")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1 and body["items"][0]["taboo_count"] == 50
    res = client.post(f"/admin/api/merge-tasks/{tid}/resolve",
                      json={"status": "done", "note": "已归并"})
    assert res.status_code == 200 and res.json()["status"] == "done"
    assert client.get("/admin/api/merge-tasks").json()["total"] == 0
    # 已消解任务重复操作 → 404
    res = client.post(f"/admin/api/merge-tasks/{tid}/resolve", json={"status": "done"})
    assert res.status_code == 404


def test_fix_body_reembeds_vector(tmp_path):
    app, client, db_path = _make_app(
        tmp_path, embedder=FakeEmbedder(), embedding_enabled=True)
    with closing(get_conn(db_path)) as conn:
        entry_id = _insert_entry(conn, topic="style")
        key = entry_id
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (key, "webchat", "u1", json.dumps([0.9, 0.9, 0.9, 0.9])),
        )
        conn.commit()
    res = client.post("/admin/api/review/decision", json={
        "user_source": "webchat", "user_id": "u1",
        "entry_id": entry_id, "decision": "fix", "entry_version": VERSION,
        "body": "fixed body",
    })
    assert res.status_code == 200, res.text
    assert res.json()["noop"] is False
    with closing(get_conn(db_path)) as conn:
        row = conn.execute(
            "SELECT embedding FROM embeddings WHERE entry_id=?", (key,)
        ).fetchone()
    assert row is not None
    raw = struct.unpack("<4f", row["embedding"])
    expected = [0.1, 0.2, 0.3, 0.4]
    norm = sum(v * v for v in expected) ** 0.5
    assert all(abs(a - b / norm) < 1e-6 for a, b in zip(raw, expected))


def test_fix_body_drops_vector_without_embedder(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        init_schema(conn, 4)
        entry_id = _insert_entry(conn, topic="style")
        key = entry_id
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (key, "webchat", "u1", json.dumps([0.9, 0.9, 0.9, 0.9])),
        )
        conn.commit()
    res = client.post("/admin/api/review/decision", json={
        "user_source": "webchat", "user_id": "u1",
        "entry_id": entry_id, "decision": "fix", "entry_version": VERSION,
        "body": "fixed body",
    })
    assert res.status_code == 200, res.text
    with closing(get_conn(db_path)) as conn:
        assert conn.execute(
            "SELECT 1 FROM embeddings WHERE entry_id=?", (key,)
        ).fetchone() is None


def test_confirm_rekey_keeps_vector(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        init_schema(conn, 4)
        eid = _insert_entry(conn, dimension="taboo", topic="不要表格", body="不要用表格")
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (eid, "webchat", "u1", json.dumps([0.1, 0.2, 0.3, 0.4])),
        )
        conn.commit()
    res = _decide(client, eid, "output_format")
    assert res.status_code == 200, res.text
    assert res.json()["rekeyed_to"] == "output_format"
    with closing(get_conn(db_path)) as conn:
        assert conn.execute(
            "SELECT 1 FROM embeddings WHERE entry_id=?", (eid,)
        ).fetchone() is not None


def test_admin_review_writes_alias(tmp_path):
    tax_path = tmp_path / "taxonomy.yaml"
    shutil.copy(REPO / "config" / "taxonomy.yaml", tax_path)
    app, client, db_path = _make_app(tmp_path, taxonomy_path=tax_path)
    canon = load_taxonomy(tax_path).dimensions["preference"].topics[0]
    with closing(get_conn(db_path)) as conn:
        eid = _insert_entry(conn, topic="手工模型", status="pending_review")
    r = client.post("/admin/api/review/decision",
                    json={"user_source": "webchat", "user_id": "u1",
                          "entry_id": eid, "decision": "confirm",
                          "entry_version": VERSION, "topic": canon})
    assert r.status_code == 200
    raw = yaml.safe_load(tax_path.read_text(encoding="utf-8"))
    pref = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert pref["topic_aliases"]["手工模型"] == canon
    store = client.app.state.taxonomy_store.current()
    assert store.dimensions["preference"].topic_aliases["手工模型"] == canon
