from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from pismor.admin.deps import Principal
from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.core.db import get_conn
from pismor.serving.api import create_app


REPO = Path(__file__).resolve().parents[2]
VERSION = "2026-01-01T00:00:00+00:00"


class FakeLLM:
    def chat_json(self, system, user, schema):
        return {"candidates": []}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "admin.db",
        taxonomy_path=REPO / "config" / "taxonomy.yaml",
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(
            enabled=False, endpoint="", model="", api_key_env="",
            dimensions=4, batch_size=8,
        ),
    )


def _make_app(tmp_path: Path):
    app = create_app(_settings(tmp_path), llm=FakeLLM())
    return app, TestClient(app), tmp_path / "admin.db"


def _insert_entry(
    conn,
    *,
    user_source="webchat",
    user_id="u1",
    dimension="preference",
    topic="style",
    status="active",
    conflict_with=None,
    summary="summary",
    body="body",
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


def test_users_list_detail_and_tombstones(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        entry_id = _insert_entry(conn, topic="a", summary="alpha")
        _insert_entry(conn, topic="b", dimension="fact", summary="beta")
        _insert_entry(conn, user_id="u2", topic="c", dimension="context", summary="gamma")
        conn.execute(
            "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
            " VALUES (?,?,?,?,?,?,?)",
            (entry_id, "webchat", "u1", "message", "m1", "原文", "explicit"),
        )
        conn.execute(
            "INSERT INTO tombstones (user_source,user_id,deleted_at,requested_by)"
            " VALUES (?,?,?,?)",
            ("webchat", "gone", VERSION, "reviewer"),
        )
        conn.commit()

    res = client.get("/admin/api/users", params={"order": "entries"})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert body["items"][0]["user_id"] == "u1"
    assert body["items"][0]["entries"] == 2

    res = client.get("/admin/api/users", params={"q": "u2"})
    assert [item["user_id"] for item in res.json()["items"]] == ["u2"]

    res = client.get("/admin/api/users/tombstones")
    assert res.status_code == 200
    assert res.json()["items"][0]["user_id"] == "gone"

    res = client.get("/admin/api/users/webchat/u1")
    assert res.status_code == 200
    detail = res.json()
    assert detail["entries_count"] == 2
    assert detail["by_dimension"] == [
        {"dimension": "preference", "count": 1},
        {"dimension": "fact", "count": 1},
    ]
    entry_a = next(e for e in detail["entries"] if e["topic"] == "a")
    assert entry_a["sources_count"] == 1
    assert (entry_a["variant"], entry_a["variant_seq"]) == ("normal", 0)

    assert client.get("/admin/api/users/webchat/missing").status_code == 404


def test_user_assemble_preview_does_not_write_clarifications(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert_entry(
            conn, topic="a", conflict_with='["preference_b"]',
            summary="喜欢简洁",
        )
        _insert_entry(
            conn, topic="b", conflict_with='["preference_a"]',
            summary="喜欢详细",
        )

    res = client.get("/admin/api/users/webchat/u1/assemble")
    assert res.status_code == 200
    body = res.json()
    assert len(body["clarification_needed"]) == 1
    assert "喜欢简洁" in body["clarification_needed"][0]["question"]
    with closing(get_conn(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 0


def test_entries_search_and_facets(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert_entry(conn, topic="a", summary="alpha")
        _insert_entry(conn, topic="b", dimension="fact", summary="beta")
        _insert_entry(conn, user_id="u2", topic="c", status="pending_review", summary="gamma")

    res = client.get("/admin/api/entries", params={"q": "alpha"})
    assert res.status_code == 200
    assert res.json()["total"] == 1
    assert (res.json()["items"][0]["variant"], res.json()["items"][0]["variant_seq"]) == ("normal", 0)

    res = client.get("/admin/api/entries", params={"dimension": "fact"})
    assert res.json()["total"] == 1

    res = client.get("/admin/api/entries", params={"status": "pending_review"})
    assert res.json()["items"][0]["user_id"] == "u2"

    res = client.get("/admin/api/entries", params={"user_id": "u2"})
    assert res.json()["total"] == 1
    assert res.json()["items"][0]["user_id"] == "u2"

    res = client.get(
        "/admin/api/entries", params={"user_source": "webchat", "user_id": "u1"}
    )
    assert res.json()["total"] == 2

    facets = client.get("/admin/api/entries/facets").json()
    assert {item["value"] for item in facets["dimensions"]} == {"preference", "fact"}
    assert {item["value"] for item in facets["statuses"]} == {"active", "pending_review"}


def test_delete_user_writes_tombstone_and_requires_capability(tmp_path):
    app, client, db_path = _make_app(tmp_path)
    with closing(get_conn(db_path)) as conn:
        _insert_entry(conn, topic="a")

    res = client.delete("/admin/api/users/webchat/u1")
    assert res.status_code == 200 and res.json()["mode"] == "soft"
    with closing(get_conn(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        row = conn.execute(
            "SELECT requested_by, purged_at FROM tombstones"
            " WHERE user_source='webchat' AND user_id='u1'"
        ).fetchone()
    assert row["requested_by"] == "local-admin" and row["purged_at"] is None

    # 软删除用户从普通列表隐藏，但仍能在删除记录和详情中查看
    assert client.get("/admin/api/users").json()["total"] == 0
    tombs = client.get("/admin/api/users/tombstones").json()
    assert tombs["total"] == 1 and tombs["items"][0]["purged_at"] is None
    detail = client.get("/admin/api/users/webchat/u1")
    assert detail.status_code == 200 and detail.json()["entries_count"] == 1

    assert client.post("/admin/api/users/webchat/u1/restore").json() == {"restored": True}
    assert client.get("/admin/api/users").json()["total"] == 1
    assert client.get("/admin/api/users/tombstones").json()["total"] == 0

    assert client.delete("/admin/api/users/webchat/u1").status_code == 200
    assert client.delete("/admin/api/users/webchat/u1/permanent").json() == {"purged": True}
    with closing(get_conn(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        purged = conn.execute(
            "SELECT purged_at FROM tombstones WHERE user_source='webchat' AND user_id='u1'"
        ).fetchone()
    assert purged["purged_at"] is not None
    assert client.post("/admin/api/users/webchat/u1/restore").status_code == 409

    app.state.principal = Principal(
        subject="nobody", display_name="无权限", roles=(), capabilities=frozenset()
    )
    assert client.delete("/admin/api/users/webchat/u2").status_code == 403
    assert client.post("/admin/api/users/webchat/u1/restore").status_code == 403
    assert client.delete("/admin/api/users/webchat/u1/permanent").status_code == 403
