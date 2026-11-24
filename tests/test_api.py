"""Task 13 API 测试：FastAPI TestClient + FakeLLM/Embedder 依赖注入（brief Step 1）。"""
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from mem.api import create_app
from mem.config import EmbeddingSettings, LLMSettings, Settings

REPO = Path(__file__).resolve().parent.parent
NOW = "2026-11-24T10:00:00+08:00"

CONV_EVENT = {
    "source": "webchat", "user_ref": "u1", "event_id": "evt_1", "event_type": "conversation",
    "session_id": "s1", "turn": 1, "ts": NOW,
    "messages": [{"role": "user", "text": "以后都用 Python"},
                 {"role": "assistant", "text": "好的"}],
}
TEXT_EVENT = {
    "source": "docs", "user_ref": "u1", "event_id": "evt_t", "event_type": "text",
    "content": "我的技术笔记", "ts": NOW, "origin_ref": "doc_42",
}

EXPLICIT_CAND = {"dimension": "preference", "topic": "lang", "scope": "global",
                 "body": "主语言 Python", "summary": "主语言Python", "confidence": 0.9,
                 "kind": "explicit", "quote": "以后都用 Python", "topic_new": None}


class FakeLLM:
    """按 schema.name 分发的假 LLM：每类可配置响应队列，缺省返回空安全值。"""

    def __init__(self):
        self.by_name = {}
        self.calls = []

    def chat_json(self, system, user, schema):
        name = schema.get("name")
        self.calls.append(name)
        queue = self.by_name.get(name)
        if queue:
            return queue.pop(0)
        return {"candidates": []} if name == "extract_candidates" else {}


class EmbedStub:
    def embed(self, texts):
        return [[0.0] * 4 for _ in texts]


@pytest.fixture
def fake_settings(tmp_path):
    return Settings(
        db_path=tmp_path / "api.db",
        taxonomy_path=REPO / "config" / "taxonomy.yaml",
        export_root=tmp_path / "exports",
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(enabled=False, endpoint="", model="",
                                    api_key_env="", dimensions=4, batch_size=8))


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def embedder_stub():
    return EmbedStub()


@pytest.fixture
def app(fake_settings, fake_llm, embedder_stub):
    return create_app(fake_settings, llm=fake_llm, embedder=embedder_stub)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def conn(app):
    return app.state.conn


@pytest.fixture
def wait_for_pipeline():
    # TestClient 同步执行 BackgroundTasks：post 返回时管线已跑完，无需等待
    return lambda: None


def _seed_entry(conn, dim, topic, scope, status, body, summary, conf,
                now=NOW, conflict_with=None, degraded_reason=None):
    entry_id = f"{dim}_{topic}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        " body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,conflict_with)"
        " VALUES ('webchat','u1',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dim, topic, scope, entry_id, conf, status, body, summary, now, now, now,
         degraded_reason, conflict_with))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"{entry_id}@{scope}", "webchat", "u1", "conversation", "seed", f"quote-{topic}",
         "explicit"))
    conn.commit()
    return entry_id


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "db": "ok"}


def test_events_accepted_202(client):
    r = client.post("/events", json=CONV_EVENT)
    assert r.status_code == 202
    assert r.json()["event_id"] == "evt_1"


def test_duplicate_200(client):
    client.post("/events", json=CONV_EVENT)
    r = client.post("/events", json=CONV_EVENT)
    assert r.status_code == 200 and r.json()["deduplicated"] is True


def test_malformed_422(client):  # 缺 user_ref/ts → intake ValidationError → 422
    r = client.post("/events", json={"source": "w"})
    assert r.status_code == 422


def test_profile_after_pipeline(client, fake_llm, wait_for_pipeline):
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(EXPLICIT_CAND)]}]
    client.post("/events", json=CONV_EVENT)
    wait_for_pipeline()
    # 无 query → 只回必带集（assembler 设计）；带话题文本 → 词面召回相关集
    r = client.get("/profile/webchat/u1?budget=2000")
    assert r.status_code == 200 and "entries" in r.json()
    r = client.get("/profile/webchat/u1?budget=2000&query=Python")
    assert any(e["id"] == "preference_lang" for e in r.json()["entries"])


def test_review_verdict_flow(client, conn):
    entry_id = _seed_entry(conn, "preference", "style", "global", "pending_review",
                           "偏好正式文体", "正式文体", 0.7)
    queue = client.get("/review/queue").json()
    assert queue["items"][0]["entry_id"] == entry_id
    version = queue["items"][0]["entry_version"]
    r = client.post(f"/review/webchat/u1/{entry_id}/verdict",
                    json={"verdict": "approve", "entry_version": version})
    assert r.status_code == 200 and r.json()["noop"] is False
    status = conn.execute("SELECT status FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
    assert status == "active"
    history = conn.execute("SELECT verdict, actor FROM review_history").fetchall()
    assert len(history) == 1 and history[0]["verdict"] == "approve" \
        and history[0]["actor"] == "reviewer"


def test_verdict_version_conflict_noop(client, conn):
    entry_id = _seed_entry(conn, "preference", "tone", "global", "pending_review",
                           "语气偏好", "语气", 0.7)
    r = client.post(f"/review/webchat/u1/{entry_id}/verdict",
                    json={"verdict": "approve", "entry_version": "1999-01-01T00:00:00+00:00"})
    assert r.status_code == 200 and r.json()["noop"] is True
    assert conn.execute("SELECT status FROM entries WHERE id=?",
                        (entry_id,)).fetchone()[0] == "pending_review"


def test_delete_tombstone(client, conn):
    _seed_entry(conn, "taboo", "emoji", "global", "active", "不用 emoji", "禁emoji", 0.95)
    _seed_entry(conn, "preference", "tone", "global", "pending_review", "正式", "正式", 0.7)
    r = client.request("DELETE", "/profile/webchat/u1")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/profile/webchat/u1").json()["entries"] == []
    assert client.get("/review/queue").json()["items"] == []
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    tomb = conn.execute("SELECT user_source, user_id FROM tombstones").fetchone()
    assert (tomb["user_source"], tomb["user_id"]) == ("webchat", "u1")


def test_export_endpoint(client, tmp_path, fake_settings):
    _seed_entry(client.app.state.conn, "taboo", "emoji", "global", "active",
                "不用 emoji", "禁emoji", 0.95)
    fake_settings.export_root.mkdir(parents=True, exist_ok=True)
    r = client.get("/export/webchat/u1")
    assert r.status_code == 200
    assert (Path(r.json()["root"]) / "taboo.md").exists()


def test_conversation_clarification_flow(client, conn, fake_llm):
    # 矛盾对已存在 + pending 澄清问题 → 用户回话命中 → clarification 候选直通决策
    _seed_entry(conn, "preference", "style", "global", "active", "偏好正式", "正式", 0.9)
    conn.execute(
        "INSERT INTO pending_clarifications (user_source, user_id, conflicting_ids, question,"
        " issued_at) VALUES ('webchat','u1',?, '以哪种为准？', ?)",
        (json.dumps(["preference_style"]), NOW))
    conn.commit()
    fake_llm.by_name["clarification_answer_check"] = [{"is_answer": True}]
    fake_llm.by_name["extract_candidates"] = [{"candidates": []}]
    fake_llm.by_name["resolve_decision"] = [{"decision": "UPDATE", "reason": "本人确认"}]
    ev = dict(CONV_EVENT, event_id="evt_2",
              messages=[{"role": "user", "text": "以后都正式一点"}])
    client.post("/events", json=ev)
    assert conn.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 0
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM sources WHERE entry_id LIKE 'preference_style@%'").fetchall()]
    assert "clarification" in kinds


def test_text_event_skips_clarification(client, conn, fake_llm):
    # text 事件不参与澄清关联（裁决）：pending 保留、不调判定 LLM
    conn.execute(
        "INSERT INTO pending_clarifications (user_source, user_id, conflicting_ids, question,"
        " issued_at) VALUES ('docs','u1','[\"x\"]','q', ?)", (NOW,))
    conn.commit()
    client.post("/events", json=TEXT_EVENT)
    assert conn.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 1
    assert "clarification_answer_check" not in fake_llm.calls


def test_pipeline_duplicate_key_degrades_to_noop(conn, fake_llm, embedder_stub, fake_settings,
                                                 taxonomy):
    # 同 key 二次矛盾写入 → IntegrityError → 编排层逐条重放，冲突条降级跳过
    from mem.pipeline import run_pipeline
    _seed_entry(conn, "preference", "food", "global", "active", "爱吃辣", "吃辣", 0.9)
    dup = dict(EXPLICIT_CAND, dimension="preference", topic="food", quote="不吃辣了",
               body="不吃辣", summary="不吃辣")
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(dup), dict(dup)]}]
    fake_llm.by_name["resolve_decision"] = [{"decision": "CONFLICT", "reason": "r"},
                                            {"decision": "CONFLICT", "reason": "r"}]
    run_pipeline(conn, fake_settings, dict(CONV_EVENT, event_id="evt_3"),
                 taxonomy=taxonomy, llm=fake_llm, embedder=embedder_stub)
    rows = conn.execute(
        "SELECT COUNT(*) c FROM entries WHERE topic='+food'").fetchone()["c"]
    assert rows == 1  # 第二次同 key 写入被降级，不抛异常不重复落行


def test_pipeline_swallows_exceptions(conn, fake_settings, taxonomy, embedder_stub):
    from mem.pipeline import run_pipeline

    class BoomLLM:
        def chat_json(self, *a, **k):
            raise RuntimeError("llm down")

    run_pipeline(conn, fake_settings, dict(CONV_EVENT, event_id="evt_9"),
                 taxonomy=taxonomy, llm=BoomLLM(), embedder=embedder_stub)  # 不上抛
