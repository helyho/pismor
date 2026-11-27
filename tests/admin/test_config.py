import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from pismor.admin.config_service import settings_to_data, validate_settings
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
    settings = load_settings(settings_path)
    return TestClient(create_app(settings, llm=FakeLLM()))


def test_config_read_has_comments(tmp_path):
    client = _app(tmp_path)
    settings = client.get("/admin/api/config/settings")
    assert settings.status_code == 200
    body = settings.json()
    assert body["version"]
    assert "单次请求超时" in body["comments"]["llm.timeout_seconds"]
    assert body["data"]["gate"]["auto_min"] == 0.85

    taxonomy = client.get("/admin/api/config/taxonomy")
    assert taxonomy.status_code == 200
    assert "抽取提示词" in taxonomy.json()["comments"]["dimensions[].guidance"]


def test_settings_save_preserves_comments_and_updates_store(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/settings").json()
    data = doc["data"]
    data["gate"]["auto_min"] = 0.9
    response = client.put(
        "/admin/api/config/settings",
        json={"expected_version": doc["version"], "data": data},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert client.app.state.settings_store.current().gate.auto_min == 0.9
    assert "单次请求超时" in (tmp_path / "settings.yaml").read_text(encoding="utf-8")
    audit = client.get("/admin/api/system/audit").json()
    assert any(item["action"] == "settings.save" for item in audit)

    stale = client.put(
        "/admin/api/config/settings",
        json={"expected_version": doc["version"], "data": data},
    )
    assert stale.status_code == 409


def test_taxonomy_save_adds_topic_and_keeps_comments(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    data["dimensions"][0]["topics"].append("new_topic")
    response = client.put(
        "/admin/api/config/taxonomy",
        json={"expected_version": doc["version"], "data": data},
    )
    assert response.status_code == 200, response.text
    assert "new_topic" in client.app.state.taxonomy_store.current().dimensions["taboo"].topics
    assert "抽取提示词" in (tmp_path / "taxonomy.yaml").read_text(encoding="utf-8")
    backups = client.get("/admin/api/config/taxonomy/backups")
    assert backups.status_code == 200
    assert backups.json()[0]["kind"] == "taxonomy"


def test_taxonomy_validate_rejects_duplicate_priority(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    data["dimensions"][1]["priority"] = 0
    response = client.post("/admin/api/config/taxonomy/validate", json={"data": data})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert any("priority" in item for item in response.json()["errors"])


def test_taxonomy_validate_ignores_topics_outside_vocabulary(tmp_path):
    client = _app(tmp_path)
    conn = get_conn(tmp_path / "index.db")
    try:
        conn.executemany(
            "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
            "body,summary,created_at,updated_at,last_reinforced_at)"
            " VALUES ('webchat','u1',?,?,?,0.9,?,'正文','摘要',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')",
            [
                ("orphan", "周末爬山", "orphan_周末爬山", "orphan_topic"),
                ("preference", "~lang:20260101T000000Z:1", "preference_~lang:20260101T000000Z:1", "archived"),
                ("taboo", "+tool", "taboo_+tool", "active"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    doc = client.get("/admin/api/config/taxonomy").json()
    result = client.post("/admin/api/config/taxonomy/validate", json={"data": doc["data"]}).json()
    assert result["ok"] is True
    assert not result["impact"]


def test_taxonomy_disabled_topic_keeps_stock_but_blocks_hard_delete(tmp_path):
    client = _app(tmp_path)
    conn = get_conn(tmp_path / "index.db")
    try:
        conn.execute(
            "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
            "body,summary,created_at,updated_at,last_reinforced_at)"
            " VALUES ('webchat','u1','preference','lang','preference_lang',"
            "0.9,'active','主语言 Python','主语言Python','2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    pref = next(dim for dim in data["dimensions"] if dim["key"] == "preference")
    pref["topics"].remove("lang")

    removed = client.post("/admin/api/config/taxonomy/validate", json={"data": data}).json()
    assert removed["ok"] is False
    assert any("硬删除" in item for item in removed["errors"])

    pref["topics"].append("lang")
    pref["disabled_topics"] = ["lang"]
    disabled = client.post("/admin/api/config/taxonomy/validate", json={"data": data}).json()
    assert disabled["ok"] is True
    assert any("停用" in item for item in disabled["warnings"])


def test_taxonomy_disabled_topic_must_be_subset(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    pref = next(dim for dim in data["dimensions"] if dim["key"] == "preference")
    pref["disabled_topics"] = ["not_in_topics"]
    result = client.post("/admin/api/config/taxonomy/validate", json={"data": data}).json()
    assert result["ok"] is False
    assert any("不在 topics" in item for item in result["errors"])


def test_settings_validate_accepts_normalized_paths():
    settings = load_settings(Path("config/settings.example.yaml"))
    raw = settings_to_data(settings)
    raw["db_path"] = "./data/index.db"
    raw["taxonomy_path"] = "./config/taxonomy.yaml"
    raw["backup"]["root"] = "./backups"

    report = validate_settings(raw, settings)
    assert report["ok"] is True, report["errors"]
    assert not any("只读字段" in item for item in report["errors"])


def test_taxonomy_validate_rejects_alias_not_in_topics(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    dim = next(item for item in data["dimensions"] if item["key"] == "preference")
    dim.setdefault("topic_aliases", {})["错别名"] = "not_in_topics"
    result = client.post("/admin/api/config/taxonomy/validate", json={"data": data}).json()
    assert result["ok"] is False
    assert any(
        "topic_aliases" in item and "not_in_topics" in item for item in result["errors"]
    )


def test_taxonomy_validate_rejects_alias_shadowing_topic(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    dim = next(item for item in data["dimensions"] if item["key"] == "preference")
    dim.setdefault("topic_aliases", {})["lang"] = "tool"
    result = client.post("/admin/api/config/taxonomy/validate", json={"data": data}).json()
    assert result["ok"] is False
    assert any("同名" in item for item in result["errors"])


def test_taxonomy_save_persists_topic_aliases(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    dim = data["dimensions"][0]
    canonical = dim["topics"][0]
    dim.setdefault("topic_aliases", {})["测试别名"] = canonical
    response = client.put(
        "/admin/api/config/taxonomy",
        json={"expected_version": doc["version"], "data": data},
    )
    assert response.status_code == 200, response.text
    saved = client.app.state.taxonomy_store.current().dimensions[dim["key"]]
    assert saved.topic_aliases["测试别名"] == canonical
    assert "测试别名" in (tmp_path / "taxonomy.yaml").read_text(encoding="utf-8")


def test_taxonomy_validate_warns_unknown_override_field(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    identity = next(item for item in data["dimensions"] if item["key"] == "identity")
    identity["topic_overrides"]["region"]["extra_key"] = "x"
    result = client.post("/admin/api/config/taxonomy/validate", json={"data": data}).json()
    assert result["ok"] is True
    assert any("extra_key" in item for item in result["warnings"])


def test_taxonomy_validate_warns_alias_to_disabled_topic(tmp_path):
    client = _app(tmp_path)
    doc = client.get("/admin/api/config/taxonomy").json()
    data = doc["data"]
    pref = next(item for item in data["dimensions"] if item["key"] == "preference")
    pref["disabled_topics"] = ["lang"]
    pref.setdefault("topic_aliases", {})["主语言"] = "lang"
    result = client.post("/admin/api/config/taxonomy/validate", json={"data": data}).json()
    assert result["ok"] is True
    assert any("别名会被丢弃" in item for item in result["warnings"])
