"""Task 13 API 测试：FastAPI TestClient + FakeLLM/Embedder 依赖注入（brief Step 1）。"""
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from mem.api import create_app
from mem.config import EmbeddingSettings, LLMSettings, Settings
from mem.backup import backup
from mem.db import get_conn, init_schema
from mem.pipeline import run_pipeline
from mem.trigger import should_extract

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
def conn(app, fake_settings):
    # 独立短连接（I-1 后 app 不持共享连接）：WAL 下 seed 提交对路由连接可见
    c = get_conn(fake_settings.db_path)
    yield c
    c.close()


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


def test_export_endpoint(client, conn, fake_settings):
    _seed_entry(conn, "taboo", "emoji", "global", "active",
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
    class BoomLLM:
        def chat_json(self, *a, **k):
            raise RuntimeError("llm down")

    run_pipeline(conn, fake_settings, dict(CONV_EVENT, event_id="evt_9"),
                 taxonomy=taxonomy, llm=BoomLLM(), embedder=embedder_stub)  # 不上抛


def test_trigger_grading_plain_message_skips_llm(client, conn, fake_llm):
    # I-4：无关键词/短消息/有画像历史 → 不调抽取 LLM，仅批内强化（简化为跳过）
    _seed_entry(conn, "preference", "lang", "global", "active", "主语言 Python",
                "主语言Python", 0.9)  # has_history=True
    plain = dict(CONV_EVENT, event_id="evt_g1",
                 messages=[{"role": "user", "text": "今天天气不错"}])
    client.post("/events", json=plain)
    assert "extract_candidates" not in fake_llm.calls


def test_trigger_grading_explicit_and_feedback_extract(client, conn, fake_llm):
    # I-4：explicit 关键词与 feedback 非空 → 直通抽取
    _seed_entry(conn, "preference", "lang", "global", "active", "主语言 Python",
                "主语言Python", 0.9)
    explicit_ev = dict(CONV_EVENT, event_id="evt_g2",
                       messages=[{"role": "user", "text": "记住我喜欢深色主题"}])
    client.post("/events", json=explicit_ev)
    assert "extract_candidates" in fake_llm.calls
    fb_ev = dict(CONV_EVENT, event_id="evt_g3",
                 messages=[{"role": "user", "text": "就这样吧"}],
                 feedback=[{"kind": "dislike", "turn": 1, "detail": "太啰嗦"}])
    client.post("/events", json=fb_ev)
    assert fake_llm.calls.count("extract_candidates") == 2


def test_trigger_grading_rules():
    # 规则表逐项钉死：(a)explicit (b)feedback (c)首查 (d)长度突变 (e)无变化
    short = [{"role": "user", "text": "嗯"}]
    assert should_extract({"messages": short}, False) == (True, "first_query")
    assert should_extract({"messages": short}, True) == (False, "no_change")
    assert should_extract({"messages": [{"role": "user", "text": "别再给我推广告"}]},
                          True) == (True, "explicit")
    assert should_extract({"messages": short, "feedback": [{"kind": "edit"}]},
                          True) == (True, "feedback")
    assert should_extract({"messages": [{"role": "user", "text": "长" * 301}]},
                          True) == (True, "long_message")


def _seed_conflict_pair(conn):
    """active 原行 + '+' 变形行（degraded/conflict_lost），双向互标 + 一条 pending。"""
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        " body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,conflict_with)"
        " VALUES ('webchat','u1','preference','style','global','preference_style',0.9,'active',"
        " '偏好正式','正式',?, ?, ?, NULL, ?)", (NOW, NOW, NOW, '["preference_+style"]'))
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        " body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,conflict_with)"
        " VALUES ('webchat','u1','preference','+style','global','preference_+style',0.72,"
        " 'degraded','偏好随意','随意',?, ?, ?, 'conflict_lost', ?)",
        (NOW, NOW, NOW, '["preference_style"]'))
    conn.execute(
        "INSERT INTO pending_clarifications (user_source, user_id, conflicting_ids, question,"
        " issued_at) VALUES ('webchat','u1','[\"preference_style\"]','以哪种为准？',?)", (NOW,))
    conn.commit()


def test_clarification_closes_conflict_loop(client, conn, fake_llm):
    # C-1：澄清回答 → UPDATE → 双侧 conflict_with 清空、对方恢复 active、pending 删净、
    # 装配 clarification_needed 为空（spec §11 闭环）
    _seed_conflict_pair(conn)
    fake_llm.by_name["clarification_answer_check"] = [{"is_answer": True}]
    fake_llm.by_name["extract_candidates"] = [{"candidates": []}]
    fake_llm.by_name["resolve_decision"] = [{"decision": "UPDATE", "reason": "本人确认正式"}]
    ev = dict(CONV_EVENT, event_id="evt_c1",
              messages=[{"role": "user", "text": "以后都正式一点"}])
    client.post("/events", json=ev)
    marks = {r["topic"]: r["conflict_with"] for r in conn.execute(
        "SELECT topic, conflict_with FROM entries WHERE topic IN ('style','+style')").fetchall()}
    assert marks["style"] is None and marks["+style"] is None
    plus = conn.execute(
        "SELECT status, degraded_reason, confidence FROM entries"
        " WHERE topic='+style'").fetchone()
    assert plus["status"] == "active" and plus["degraded_reason"] is None
    assert plus["confidence"] == 0.72  # 恢复 active 置信度不变（裁决 C-1）
    assert conn.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 0
    assert client.get("/profile/webchat/u1").json()["clarification_needed"] == []


def test_verdict_advances_version_and_clears_conflict(client, conn):
    # I-2：verdict 刷 updated_at（同版本二次判定 noop）+ '+' 行收尾移出复核队列
    _seed_conflict_pair(conn)
    queue = client.get("/review/queue").json()["items"]
    assert any(i["entry_id"] == "preference_+style" for i in queue)
    version = next(i["entry_version"] for i in queue if i["entry_id"] == "preference_+style")
    r = client.post("/review/webchat/u1/preference_+style/verdict",
                    json={"verdict": "approve", "entry_version": version})
    assert r.status_code == 200 and r.json()["noop"] is False
    plus = conn.execute(
        "SELECT status, conflict_with, updated_at FROM entries WHERE topic='+style'").fetchone()
    assert plus["status"] == "active" and plus["conflict_with"] is None
    assert plus["updated_at"] != version  # 版本已推进（spec §8.1 变更即刷新）
    assert conn.execute(  # 对方行 conflict_lost 恢复 active
        "SELECT status FROM entries WHERE topic='style'").fetchone()[0] == "active"
    remaining = client.get("/review/queue").json()["items"]
    assert not any(i["entry_id"] == "preference_+style" for i in remaining)
    r2 = client.post("/review/webchat/u1/preference_+style/verdict",
                     json={"verdict": "discard", "entry_version": version})
    assert r2.status_code == 200 and r2.json()["noop"] is True  # 同版本二次判定作废
    assert conn.execute("SELECT COUNT(*) FROM review_history").fetchone()[0] == 1


def test_orphan_candidate_review_approve_to_observation(client, conn, fake_llm, fake_settings,
                                                        taxonomy, embedder_stub):
    # I-3：inferred 词表外候选落复核队列；approve → orphan_topic 进观察区，
    # 聚合晋升链可见（窗口内 1 行 → 保留等复现，不误删）
    orphan_cand = {"dimension": "orphan", "topic": "手工模型", "scope": "global",
                   "body": "喜欢做手工模型", "summary": "做手工模型", "confidence": 0.7,
                   "kind": "inferred", "quote": "我爱做手工模型",
                   "topic_new": "手工模型", "status": "orphan_topic"}
    fake_llm.by_name["extract_candidates"] = [{"candidates": [orphan_cand]}]
    client.post("/events", json=dict(CONV_EVENT, event_id="evt_o1"))
    queue = client.get("/review/queue").json()["items"]
    assert any(i["entry_id"] == "orphan_手工模型" and i["status"] == "pending_review"
               for i in queue)
    version = next(i["entry_version"] for i in queue if i["entry_id"] == "orphan_手工模型")
    client.post("/review/webchat/u1/orphan_手工模型/verdict",
                json={"verdict": "approve", "entry_version": version})
    row = conn.execute(
        "SELECT status, topic_new, degraded_reason FROM entries"
        " WHERE id='orphan_手工模型'").fetchone()
    assert row["status"] == "orphan_topic" and row["topic_new"] == "手工模型"
    from mem.aggregator import run_daily
    stats = run_daily(conn, taxonomy, fake_llm, False, "2026-12-24T00:00:00+00:00")
    still = conn.execute(
        "SELECT status FROM entries WHERE id='orphan_手工模型'").fetchone()
    assert still["status"] == "orphan_topic"  # 窗口内仅 1 行 → 保留等复现，晋升链可用
    assert stats["dropped_orphan"] == 0


def test_concurrent_pipeline_and_backup_isolated_connections(conn, fake_llm, embedder_stub,
                                                             fake_settings, taxonomy, tmp_path):
    # I-1：管线任务与备份各持独立连接并发执行，互不踩事务
    import threading
    from concurrent.futures import ThreadPoolExecutor

    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(EXPLICIT_CAND)]}]
    class RepeatLLM:
        def chat_json(self, system, user, schema):  # 不限量：多线程并发消费互不影响
            return {"candidates": [dict(EXPLICIT_CAND)]}

    errors: list[Exception] = []

    def work(ev):
        try:
            c = get_conn(fake_settings.db_path)
            try:
                run_pipeline(c, fake_settings, ev, taxonomy=taxonomy,
                             llm=RepeatLLM(), embedder=embedder_stub)
            finally:
                c.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def backup_work():
        try:
            c = get_conn(fake_settings.db_path)
            try:
                backup(c, tmp_path / "bk")
            finally:
                c.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    events = [dict(CONV_EVENT, event_id=f"evt_p{i}", user_ref=f"p{i}") for i in range(2)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(work, ev) for ev in events] + [pool.submit(backup_work)]
        for f in futures:
            f.result()
    assert errors == []
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    assert len(list((tmp_path / "bk").glob("backup-*.db"))) == 1


def test_backup_vacuum_before_cleanup(conn, tmp_path):
    # 顺手②：VACUUM 成功落位后才清理；同日重跑原子覆盖既有快照
    target_dir = tmp_path / "bk"
    p1 = backup(conn, target_dir)
    old = target_dir / "backup-20250101.db"
    old.write_text("stale")
    for i in range(2, 34):  # 20250102..20250133：同格式八位日期，字典序=时间序
        (target_dir / f"backup-202501{i:02d}.db").write_text("stale")
    p2 = backup(conn, target_dir)  # 同日重跑：33 份旧 + 当日 = 34 份 → 保留最新 30
    assert p1 == p2 and p2.exists() and p2.stat().st_size > 0
    assert old.exists() is False  # 30 份外清理生效
    assert len(list(target_dir.glob("backup-*.db"))) == 30
