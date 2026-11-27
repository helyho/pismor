import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from pismor.core.config import load_settings
from pismor.core.db import get_conn
from pismor.serving.api import create_app


class FakeLLM:
    def chat_json(self, system, user, schema):
        return {"candidates": []}


def _app(tmp_path: Path) -> TestClient:
    settings_path = tmp_path / "settings.yaml"
    shutil.copy(Path("config/settings.example.yaml"), settings_path)
    text = settings_path.read_text(encoding="utf-8")
    text = text.replace("./data/index.db", str(tmp_path / "index.db"))
    text = text.replace("./config/taxonomy.yaml", str(tmp_path / "taxonomy.yaml"))
    text = text.replace("  enabled: true", "  enabled: false")
    text = text.replace("root: ./backups", f"root: {tmp_path / 'backups'}")
    settings_path.write_text(text, encoding="utf-8")
    shutil.copy(Path("config/taxonomy.yaml"), tmp_path / "taxonomy.yaml")
    return TestClient(create_app(load_settings(settings_path), llm=FakeLLM()))


def _seed(app: TestClient) -> None:
    db_path = app.app.state.settings_store.current().db_path
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
            "body,summary,created_at,updated_at,last_reinforced_at)"
            " VALUES ('webchat','u1','preference','theme','preference_theme',0.9,"
            "'active','喜欢深色','喜欢深色','2026-11-20T00:00:00+00:00',"
            "'2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
            "body,summary,created_at,updated_at,last_reinforced_at)"
            " VALUES ('webchat','u2','fact','city','fact_city',0.7,"
            "'pending_review','上海','上海','2026-11-20T00:00:00+00:00',"
            "'2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO review_history (entry_id,user_source,user_id,decision,actor,ts,note,entry_version)"
            " VALUES ('fact_city','webchat','u2','confirm','reviewer',"
            "'2026-11-20T12:00:00+00:00',NULL,'2026-11-20T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO job_runs (job,started_at,finished_at,ok,stats_json)"
            " VALUES ('daily','2026-11-20T00:00:00+00:00','2026-11-20T00:00:01+00:00',1,"
            "'{\"activated\": 1, \"flagged\": 0}')"
        )
        conn.execute(
            "INSERT INTO admin_audit (actor,action,target,ts,detail)"
            " VALUES ('local-admin','config.save','config/settings.yaml',"
            "'2026-11-20T00:00:00+00:00','gate.auto_min')"
        )
        conn.commit()


def test_system_health_jobs_stats_audit(tmp_path):
    client = _app(tmp_path)
    _seed(client)
    health = client.get("/admin/api/system/health")
    assert health.status_code == 200
    assert health.json()["db_ok"] is True
    assert health.json()["entries"] == 2
    assert health.json()["pending_review"] == 1

    jobs = client.get("/admin/api/system/jobs")
    assert jobs.status_code == 200
    assert jobs.json()[0]["ok"] is True
    assert jobs.json()[0]["stats"]["activated"] == 1

    stats = client.get("/admin/api/system/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert any(item["status"] == "active" and item["count"] == 1 for item in body["by_status"])
    assert any(item["count"] == 1 for item in body["review_trend"])
    assert body["top_topics"][0]["topic"] == "theme"
    assert body["pending_backlog"][0]["count"] == 1

    audit = client.get("/admin/api/system/audit")
    assert audit.status_code == 200
    assert audit.json()[0]["action"] == "config.save"


def test_system_capability_required(tmp_path):
    client = _app(tmp_path)
    client.app.state.principal = client.app.state.principal.__class__(
        "viewer", "只读", ("viewer",), frozenset()
    )
    assert client.get("/admin/api/system/health").status_code == 403
