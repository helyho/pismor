from pathlib import Path

from fastapi.testclient import TestClient

from pismor.admin.deps import Principal
from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.serving.api import create_app


REPO = Path(__file__).resolve().parents[2]


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


def test_me_returns_local_admin(tmp_path):
    app = create_app(_settings(tmp_path), llm=FakeLLM())
    res = TestClient(app).get("/admin/api/me")
    assert res.status_code == 200
    body = res.json()
    assert body["subject"] == "local-admin"
    assert "system.read" in body["capabilities"]


def test_overview_empty_db(tmp_path):
    app = create_app(_settings(tmp_path), llm=FakeLLM())
    res = TestClient(app).get("/admin/api/overview")
    assert res.status_code == 200
    body = res.json()
    assert body["entries"] == 0
    assert body["users"] == 0
    assert body["pending_review"] == 0
    assert body["by_status"] == []
    assert body["by_dimension"] == []
    assert body["updated_at"] is None


def test_overview_requires_capability(tmp_path):
    app = create_app(_settings(tmp_path), llm=FakeLLM())
    app.state.principal = Principal(
        subject="nobody", display_name="无权限", roles=(), capabilities=frozenset()
    )
    res = TestClient(app).get("/admin/api/overview")
    assert res.status_code == 403
    assert "system.read" in res.json()["detail"]
