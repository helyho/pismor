from pathlib import Path

from fastapi.testclient import TestClient

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


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id='app'></div>", encoding="utf-8")
    return dist


def test_spa_fallback_serves_index(tmp_path, monkeypatch):
    monkeypatch.setenv("PISMOR_ADMIN_DIST", str(_dist(tmp_path)))
    app = create_app(_settings(tmp_path), llm=FakeLLM())
    client = TestClient(app)
    assert "<div id='app'></div>" in client.get("/admin/").text
    assert "<div id='app'></div>" in client.get("/admin/review").text


def test_admin_api_is_not_shadowed_by_spa(tmp_path, monkeypatch):
    monkeypatch.setenv("PISMOR_ADMIN_DIST", str(_dist(tmp_path)))
    app = create_app(_settings(tmp_path), llm=FakeLLM())
    res = TestClient(app).get("/admin/api/me")
    assert res.status_code == 200
    assert res.json()["subject"] == "local-admin"
