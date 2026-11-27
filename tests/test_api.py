"""Task 13 API 测试：FastAPI TestClient + FakeLLM/Embedder 依赖注入（brief Step 1）。"""
from contextlib import closing
from pathlib import Path
import json
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from pismor.serving.api import create_app
from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.serving.backup import backup
from pismor.core.db import get_conn, init_schema
from pismor.core.taxonomy import load_taxonomy
from pismor.pipeline.runner import run_pipeline
from pismor.pipeline.normalize import normalize
from pismor.pipeline.trigger import build_explicit_re, should_extract as _should_extract


def should_extract(event, *args, **kwargs):
    """测试适配器：用例以原始事件 dict 表达输入，先经 normalize 得到管线视图。"""
    if "event_type" not in event:
        event = {**event, "event_type": "conversation"}
    return _should_extract(normalize(event), *args, **kwargs)

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

EXPLICIT_CAND = {"dimension": "preference", "topic": "lang",
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


def _seed_entry(conn, dim, topic, status, body, summary, conf,
                now=NOW, conflict_with=None, degraded_reason=None):
    entry_id = f"{dim}_{topic}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        " body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,conflict_with)"
        " VALUES ('webchat','u1',?,?,?,?,?,?,?,?,?,?,?,?)",
        (dim, topic, entry_id, conf, status, body, summary, now, now, now,
         degraded_reason, conflict_with))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (entry_id, "webchat", "u1", "conversation", "seed", f"quote-{topic}",
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


def test_control_char_user_ref_422(client):  # F5：键成分含 \x1f 会撞 vec_key，422 拒收
    ev = dict(CONV_EVENT, user_ref="u\x1fa")
    assert client.post("/events", json=ev).status_code == 422


def test_assistant_only_conversation_422(client):
    # 入口硬约束：messages 必须含至少一条 user，否则 422（不落库、不进管线）
    ev = dict(CONV_EVENT, event_id="evt_no_user",
              messages=[{"role": "assistant", "text": "好的"}])
    assert client.post("/events", json=ev).status_code == 422


def test_memory_after_pipeline(client, fake_llm, wait_for_pipeline):
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(EXPLICIT_CAND)]}]
    client.post("/events", json=CONV_EVENT)
    wait_for_pipeline()
    # 无 query → 只回必带集（assembler 设计）；带话题文本 → 词面召回相关集
    r = client.get("/memory/webchat/u1?budget=2000")
    assert r.status_code == 200 and "entries" in r.json()
    r = client.get("/memory/webchat/u1?budget=2000&query=Python")
    assert any("Python" in e["text"] for e in r.json()["entries"])


def test_review_decision_flow(client, conn):
    entry_id = _seed_entry(conn, "preference", "style", "pending_review",
                           "偏好正式文体", "正式文体", 0.7)
    queue = client.get("/review/queue").json()
    assert queue["items"][0]["entry_id"] == entry_id
    version = queue["items"][0]["entry_version"]
    r = client.post(f"/review/webchat/u1/{entry_id}/decision",
                    json={"decision": "confirm", "entry_version": version})
    assert r.status_code == 200 and r.json()["noop"] is False
    status = conn.execute("SELECT status FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
    assert status == "active"
    history = conn.execute("SELECT decision, actor FROM review_history").fetchall()
    assert len(history) == 1 and history[0]["decision"] == "confirm" \
        and history[0]["actor"] == "reviewer"


def test_decision_version_conflict_noop(client, conn):
    entry_id = _seed_entry(conn, "preference", "tone", "pending_review",
                           "语气偏好", "语气", 0.7)
    r = client.post(f"/review/webchat/u1/{entry_id}/decision",
                    json={"decision": "confirm", "entry_version": "1999-01-01T00:00:00+00:00"})
    assert r.status_code == 200 and r.json()["noop"] is True
    assert conn.execute("SELECT status FROM entries WHERE id=?",
                        (entry_id,)).fetchone()[0] == "pending_review"


def test_delete_tombstone(client, conn):
    _seed_entry(conn, "taboo", "emoji", "active", "不用 emoji", "禁emoji", 0.95)
    _seed_entry(conn, "preference", "tone", "pending_review", "正式", "正式", 0.7)
    r = client.request("DELETE", "/memory/webchat/u1")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/memory/webchat/u1").json()["entries"] == []
    assert client.get("/review/queue").json()["items"] == []
    # 软删除：记忆数据保留，但公开读取与队列都按删除状态隐藏
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] >= 2
    tomb = conn.execute(
        "SELECT user_source, user_id, purged_at FROM tombstones").fetchone()
    assert (tomb["user_source"], tomb["user_id"]) == ("webchat", "u1")
    assert tomb["purged_at"] is None


def test_conversation_clarification_flow(client, conn, fake_llm):
    # 矛盾对已存在 + pending 澄清问题 → 用户回话命中 → clarification 候选直通决策
    _seed_entry(conn, "preference", "style", "active", "偏好正式", "正式", 0.9)
    conn.execute(
        "INSERT INTO pending_clarifications (user_source, user_id, conflicting_ids, question,"
        " issued_at) VALUES ('webchat','u1',?, '以哪种为准？', ?)",
        (json.dumps(["preference_style"]), NOW))
    conn.commit()
    fake_llm.by_name["clarification_answer_check"] = [{"is_answer": True}]
    fake_llm.by_name["clarify_candidates"] = [{"candidates": [{
        "dimension": "preference", "topic": "style",
        "body": "回复保持正式书面语", "summary": "正式书面语", "confidence": 0.95,
        "kind": "explicit", "quote": "以后都正式一点", "topic_new": None}]}]
    ev = dict(CONV_EVENT, event_id="evt_2",
              messages=[{"role": "user", "text": "以后都正式一点"}])
    client.post("/events", json=ev)
    assert conn.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 0
    row = conn.execute("SELECT body FROM entries WHERE topic='style'").fetchone()
    assert row["body"] == "回复保持正式书面语"  # 规范化陈述，非用户原句
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM sources WHERE entry_id='preference_style'").fetchall()]
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
    _seed_entry(conn, "preference", "style", "active", "爱吃辣", "吃辣", 0.9)
    dup = dict(EXPLICIT_CAND, dimension="preference", topic="style", quote="不吃辣了",
               body="不吃辣", summary="不吃辣")
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(dup), dict(dup)]}]
    fake_llm.by_name["resolve_decision"] = [{"decision": "CONFLICT", "reason": "r"},
                                            {"decision": "CONFLICT", "reason": "r"}]
    run_pipeline(conn, fake_settings, dict(CONV_EVENT, event_id="evt_3"),
                 taxonomy=taxonomy, llm=fake_llm, embedder=embedder_stub)
    rows = conn.execute(
        "SELECT COUNT(*) c FROM entries WHERE topic='style' AND variant='conflict'"
    ).fetchone()["c"]
    assert rows == 2  # 同 key 多次冲突各占一个 variant_seq，不再撞主键


def test_pipeline_swallows_exceptions(conn, fake_settings, taxonomy, embedder_stub):
    class BoomLLM:
        def chat_json(self, *a, **k):
            raise RuntimeError("llm down")

    run_pipeline(conn, fake_settings, dict(CONV_EVENT, event_id="evt_9"),
                 taxonomy=taxonomy, llm=BoomLLM(), embedder=embedder_stub)  # 不上抛


def test_trigger_grading_plain_message_skips_llm(client, conn, fake_llm):
    # I-4：无关键词/短消息/有记忆历史 → 不调抽取 LLM，仅批内强化（简化为跳过）
    _seed_entry(conn, "preference", "lang", "active", "主语言 Python",
                "主语言Python", 0.9)  # has_history=True
    plain = dict(CONV_EVENT, event_id="evt_g1",
                 messages=[{"role": "user", "text": "今天天气不错"}])
    client.post("/events", json=plain)
    assert "extract_candidates" not in fake_llm.calls


def test_trigger_grading_explicit_and_forced_extract(client, conn, fake_llm):
    # I-4：explicit 关键词与 force_extract → 直通抽取
    _seed_entry(conn, "preference", "lang", "active", "主语言 Python",
                "主语言Python", 0.9)
    explicit_ev = dict(CONV_EVENT, event_id="evt_g2",
                       messages=[{"role": "user", "text": "记住我喜欢深色主题"}])
    client.post("/events", json=explicit_ev)
    assert "extract_candidates" in fake_llm.calls
    fb_ev = dict(CONV_EVENT, event_id="evt_g3",
                 messages=[{"role": "user", "text": "就这样吧"}],
                 force_extract=True)
    client.post("/events", json=fb_ev)
    assert fake_llm.calls.count("extract_candidates") == 2


def test_trigger_grading_rules():
    # 规则表逐项钉死：(a)explicit (b)long_message (c)force_extract (d)首查 (e)无变化
    short = [{"role": "user", "text": "嗯"}]
    assert should_extract({"messages": short}, False) == (True, "first_query")
    assert should_extract({"messages": short}, True) == (False, "no_change")
    assert should_extract({"messages": [{"role": "user", "text": "别再给我推广告"}]},
                          True) == (True, "explicit")
    assert should_extract({"messages": short, "force_extract": True},
                          True) == (True, "forced")
    assert should_extract({"messages": [{"role": "user", "text": "长" * 301}]},
                          True) == (True, "long_message")


def test_trigger_grading_force_extract_needs_user_text():
    # 防御性单测（绕过 intake 直调）：API 入口已由 schema 422 拦截 assistant-only
    assert should_extract({"messages": [{"role": "assistant", "text": "好的"}],
                           "force_extract": True}, True) == (False, "no_change")


def test_force_extract_string_false_does_not_trigger(client, conn, fake_llm):
    # lax 校验 "False" → False，intake 归一化回写；管线不得把原始字符串当 truthy
    _seed_entry(conn, "preference", "lang", "active", "主语言 Python",
                "主语言Python", 0.9)
    ev = dict(CONV_EVENT, event_id="evt_false",
              messages=[{"role": "user", "text": "就这样吧"}],
              force_extract="False")
    client.post("/events", json=ev)
    assert "extract_candidates" not in fake_llm.calls


def test_trigger_explicit_keywords_english_default():
    # 默认词表含英文声明关键词；普通英文句子不误触发
    en = [{"role": "user", "text": "Remember that I prefer dark mode"}]
    assert should_extract({"messages": en}, True) == (True, "explicit")
    assert should_extract({"messages": [{"role": "user", "text": "From now on use Python"}]},
                          True) == (True, "explicit")
    # 词边界保护：含 "stop" 子串的普通词不命中
    assert should_extract({"messages": [{"role": "user", "text": "the stopwatch works"}]},
                          True) == (False, "no_change")


def test_trigger_explicit_keywords_custom_vocab():
    # 自定义词表生效：仅命中配置词；词表为空则 explicit 规则关闭
    custom = should_extract(
        {"messages": [{"role": "user", "text": "请固定用简体中文回复"}]}, True,
        explicit_re=build_explicit_re(["固定", "固定用"]))
    assert custom == (True, "explicit")
    empty = should_extract(
        {"messages": [{"role": "user", "text": "别再给我推广告"}]}, True,
        explicit_re=build_explicit_re([]))
    assert empty == (False, "no_change")


def _seed_conflict_pair(conn):
    """normal 行 + conflict 变体（同 topic），双向互标 + 一条 pending。"""
    main_id, plus_id = "preference_style_main", "preference_style_conflict"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,variant,variant_seq,"
        " confidence,status,body,summary,created_at,updated_at,last_reinforced_at,"
        " degraded_reason,conflict_with)"
        " VALUES ('webchat','u1','preference','style',?, 'normal',0,0.9,'active',"
        " '偏好正式','正式',?,?,?, NULL, ?)",
        (main_id, NOW, NOW, NOW, json.dumps([plus_id])))
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,variant,variant_seq,"
        " confidence,status,body,summary,created_at,updated_at,last_reinforced_at,"
        " degraded_reason,conflict_with)"
        " VALUES ('webchat','u1','preference','style',?, 'conflict',1,0.72,"
        " 'degraded','偏好随意','随意',?,?,?, 'conflict_lost', ?)",
        (plus_id, NOW, NOW, NOW, json.dumps([main_id])))
    conn.execute(
        "INSERT INTO pending_clarifications (user_source, user_id, conflicting_ids, question,"
        " issued_at) VALUES ('webchat','u1',?, '以哪种为准？',?)",
        (json.dumps([main_id, plus_id]), NOW))
    conn.commit()


def test_clarification_closes_conflict_loop(client, conn, fake_llm):
    # C-1：澄清回答 → UPDATE → 双侧 conflict_with 清空、对方恢复 active、pending 删净、
    # 装配 clarification_needed 为空（spec §11 闭环）
    _seed_conflict_pair(conn)
    fake_llm.by_name["clarification_answer_check"] = [{"is_answer": True}]
    fake_llm.by_name["clarify_candidates"] = [{"candidates": [{
        "dimension": "preference", "topic": "style",
        "body": "回复保持正式书面语", "summary": "正式书面语", "confidence": 0.95,
        "kind": "explicit", "quote": "以后都正式一点", "topic_new": None}]}]
    ev = dict(CONV_EVENT, event_id="evt_c1",
              messages=[{"role": "user", "text": "以后都正式一点"}])
    client.post("/events", json=ev)
    marks = {r["variant"]: r["conflict_with"] for r in conn.execute(
        "SELECT variant, conflict_with FROM entries WHERE topic='style'").fetchall()}
    assert marks["normal"] is None and marks["conflict"] is None
    plus = conn.execute(
        "SELECT status, degraded_reason, confidence, archived_at FROM entries"
        " WHERE topic='style' AND variant='conflict'").fetchone()
    # '+' 变形行按裁决归档为轨迹，不得原样 active（C-2/T14）
    assert plus["status"] == "archived" and plus["archived_at"] is not None
    main = conn.execute(
        "SELECT body, status FROM entries WHERE topic='style' AND variant='normal'").fetchone()
    assert main["status"] == "active" and main["body"] == "回复保持正式书面语"
    assert conn.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 0
    assert client.get("/memory/webchat/u1").json()["clarification_needed"] == []


def test_decision_advances_version_and_clears_conflict(client, conn):
    # I-2：decision 刷 updated_at（同版本二次判定 noop）+ '+' 行收尾移出复核队列
    _seed_conflict_pair(conn)
    plus_id = conn.execute(
        "SELECT id FROM entries WHERE topic='style' AND variant='conflict'").fetchone()[0]
    queue = client.get("/review/queue").json()["items"]
    assert any(i["entry_id"] == plus_id for i in queue)
    version = next(i["entry_version"] for i in queue if i["entry_id"] == plus_id)
    r = client.post(f"/review/webchat/u1/{plus_id}/decision",
                    json={"decision": "confirm", "entry_version": version})
    assert r.status_code == 200 and r.json()["noop"] is False
    plus = conn.execute(
        "SELECT status, conflict_with, updated_at FROM entries"
        " WHERE topic='style' AND variant='conflict'").fetchone()
    assert plus["status"] == "active" and plus["conflict_with"] is None
    assert plus["updated_at"] != version  # 版本已推进（spec §8.1 变更即刷新）
    assert conn.execute(  # 对方行 conflict_lost 恢复 active
        "SELECT status FROM entries WHERE topic='style' AND variant='normal'"
    ).fetchone()[0] == "active"
    remaining = client.get("/review/queue").json()["items"]
    assert not any(i["entry_id"] == plus_id for i in remaining)
    r2 = client.post(f"/review/webchat/u1/{plus_id}/decision",
                     json={"decision": "remove", "entry_version": version})
    assert r2.status_code == 200 and r2.json()["noop"] is True  # 同版本二次判定作废
    assert conn.execute("SELECT COUNT(*) FROM review_history").fetchone()[0] == 1


def test_orphan_candidate_review_confirm_to_observation(client, conn, fake_llm, fake_settings,
                                                        taxonomy, embedder_stub):
    # I-3：inferred 词表外候选落复核队列；confirm → orphan_topic 进观察区，
    # 聚合启用链可见（窗口内 1 行 → 保留等复现，不误删）
    orphan_cand = {"dimension": "orphan", "topic": "手工模型",
                   "body": "喜欢做手工模型", "summary": "做手工模型", "confidence": 0.7,
                   "kind": "inferred", "quote": "我爱做手工模型",
                   "topic_new": "手工模型", "status": "orphan_topic"}
    fake_llm.by_name["extract_candidates"] = [{"candidates": [orphan_cand]}]
    client.post("/events", json=dict(CONV_EVENT, event_id="evt_o1"))
    orphan_id = conn.execute(
        "SELECT id FROM entries WHERE dimension='orphan' AND topic='手工模型'").fetchone()[0]
    queue = client.get("/review/queue").json()["items"]
    assert any(i["entry_id"] == orphan_id and i["status"] == "pending_review"
               for i in queue)
    version = next(i["entry_version"] for i in queue if i["entry_id"] == orphan_id)
    client.post(f"/review/webchat/u1/{orphan_id}/decision",
                json={"decision": "confirm", "entry_version": version})
    row = conn.execute(
        "SELECT status, topic_new, degraded_reason FROM entries"
        " WHERE id=?", (orphan_id,)).fetchone()
    assert row["status"] == "orphan_topic" and row["topic_new"] == "手工模型"
    from pismor.pipeline.aggregator import run_daily
    stats = run_daily(conn, taxonomy, fake_llm, False, "2026-12-24T00:00:00+00:00")
    still = conn.execute(
        "SELECT status FROM entries WHERE id=?", (orphan_id,)).fetchone()
    assert still["status"] == "orphan_topic"  # 窗口内仅 1 行 → 保留等复现，启用链可用
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


def test_backup_keep_configurable(conn, tmp_path):
    # keep=2：滚动保留收紧到 2 份（settings.backup.keep 可配）
    target_dir = tmp_path / "bk"
    target_dir.mkdir()
    for i in range(1, 4):
        (target_dir / f"backup-2025010{i}.db").write_text("stale")
    backup(conn, target_dir, keep=2)
    files = sorted(target_dir.glob("backup-*.db"))
    assert len(files) == 2
    assert files[0].name == "backup-20250103.db"  # 最旧两份被清，前一日留存
    assert files[1] == backup(conn, target_dir, keep=2)  # 当日快照同日重跑原子覆盖


def test_backup_keep_zero_drops_all(conn, tmp_path):
    # keep=0 = 不保留任何快照；旧实现 snapshots[:-0] 会一份都不删
    target_dir = tmp_path / "bk"
    target_dir.mkdir()
    for i in range(1, 4):
        (target_dir / f"backup-2025010{i}.db").write_text("stale")
    backup(conn, target_dir, keep=0)
    assert list(target_dir.glob("backup-*.db")) == []


# ---- fix wave 新增：decision / pending 演进 / 隔离 / 入口 / 导出 ----

def test_decision_remove_deletes_entry_and_sources(client, conn):
    # C-1：remove 物理删条目，且不误删同用户其他条目与证据
    _seed_entry(conn, "preference", "lang", "active", "主语言 Python", "主语言", 0.9)
    _seed_entry(conn, "preference", "style", "pending_review", "偏好正式", "正式", 0.7)
    queue = client.get("/review/queue").json()["items"]
    item = next(i for i in queue if i["entry_id"] == "preference_style")
    r = client.post("/review/webchat/u1/preference_style/decision",
                    json={"decision": "remove", "entry_version": item["entry_version"]})
    assert r.status_code == 200 and r.json() == {"noop": False, "updated": 1}
    ids = {row["id"] for row in conn.execute("SELECT id FROM entries").fetchall()}
    assert ids == {"preference_lang"}  # 只删被判定条目
    assert conn.execute("SELECT COUNT(*) c FROM sources WHERE entry_id='preference_style'"
                        ).fetchone()["c"] == 0


def test_decision_remove_clears_vectors(client, conn):
    # 复核中的行可能已有向量（如 active 被审计标记成 pending_review）；
    # remove 物理删条目时必须同时清 embeddings，否则留下指向不存在条目的残行
    init_schema(conn, 4)  # 模拟启用向量库的部署（app 只在 embedding.enabled 时建表）
    entry_id = _seed_entry(conn, "preference", "style", "pending_review", "正式", "正式", 0.7)
    key = entry_id
    conn.execute(
        "INSERT INTO embeddings (entry_id, user_source, user_id, embedding) VALUES (?,?,?,?)",
        (key, "webchat", "u1", json.dumps([0.1, 0.2, 0.3, 0.4])))
    conn.commit()
    item = next(i for i in client.get("/review/queue").json()["items"]
                if i["entry_id"] == entry_id)
    r = client.post(f"/review/webchat/u1/{entry_id}/decision",
                    json={"decision": "remove", "entry_version": item["entry_version"]})
    assert r.status_code == 200
    n = conn.execute("SELECT COUNT(*) c FROM embeddings WHERE entry_id=?", (key,)).fetchone()["c"]
    assert n == 0


def test_pending_review_does_not_block_evolution(client, conn, fake_llm):
    # I-2：pending_review 同 key 新证据 → active 演进；pending 快照留在复核队列
    _seed_entry(conn, "preference", "style", "pending_review", "猜测正式", "猜测", 0.7)
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(
        EXPLICIT_CAND, topic="style", body="明确偏好简洁", summary="简洁",
        quote="记住以后回复简洁")]}]
    client.post("/events", json=dict(CONV_EVENT, event_id="evt_p1",
                                     messages=[{"role": "user", "text": "记住以后回复简洁"}]))
    main = conn.execute(
        "SELECT status, body FROM entries WHERE topic='style' AND variant='normal'"
        " AND status='active'").fetchone()
    pending = conn.execute(
        "SELECT id, status, topic, variant FROM entries WHERE variant='snapshot'").fetchone()
    assert main["status"] == "active" and main["body"] == "明确偏好简洁"
    assert pending["topic"] == "style" and pending["variant"] == "snapshot"
    queue = client.get("/review/queue").json()["items"]
    assert any(i["entry_id"] == pending["id"] for i in queue)


def test_illegal_transition_isolated_not_batch_rollback(conn, fake_settings, taxonomy,
                                                        embedder_stub):
    # I-2：单条非法迁移隔离留痕，其余候选照常落库
    from pismor.pipeline import runner
    _seed_entry(conn, "preference", "old", "archived", "旧", "旧", 0.9, now=NOW)
    good = {"op": "ADD", "candidate": dict(EXPLICIT_CAND)}
    bad = {"op": "UPDATE", "candidate": dict(EXPLICIT_CAND, topic="old"),
           "target": ("preference", "old")}
    applied = runner._apply(conn, "webchat", "u1", [good, bad], embedder_stub,
                              "text", "evt_iso", NOW)
    assert [a["op"] for a in applied] == ["ADD"]
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE topic='lang'").fetchone()["c"] == 1
    failure = conn.execute(
        "SELECT decision, actor FROM review_history WHERE decision='apply_failed'").fetchone()
    assert failure["actor"] == "pipeline"
    assert conn.execute("SELECT status FROM entries WHERE topic='old'").fetchone()["status"] \
        == "archived"


def test_hash_fallback_event_id_written_to_origin_ref(client, conn, fake_llm):
    # I-13：缺省 event_id 的 hash 兜底必须回写管线，证据 origin_ref 不得为空串
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(EXPLICIT_CAND)]}]
    event = {k: v for k, v in CONV_EVENT.items() if k != "event_id"}
    r = client.post("/events", json=event)
    assert r.status_code == 202 and r.json()["event_id"].startswith("sha256:")
    ref = conn.execute("SELECT origin_ref FROM sources").fetchone()["origin_ref"]
    assert ref and ref == r.json()["event_id"]


def test_trigger_grading_reinforces_matching_entry(client, conn, fake_llm):
    # M-7：无变化短消息按话题词面命中 top-20 强化，last_reinforced_at 刷新
    _seed_entry(conn, "preference", "lang", "active", "主语言 Python",
                "主语言Python", 0.9)
    conn.execute("UPDATE entries SET reinforcements=0,"
                 " last_reinforced_at='2026-01-01T00:00:00+00:00'")
    conn.commit()
    plain = dict(CONV_EVENT, event_id="evt_m7",
                 messages=[{"role": "user", "text": "Python 用着顺手"}])
    client.post("/events", json=plain)
    row = conn.execute("SELECT reinforcements, last_reinforced_at FROM entries").fetchone()
    assert row["reinforcements"] == 1 and row["last_reinforced_at"] == NOW
    assert "extract_candidates" not in fake_llm.calls


def test_clarification_without_candidates_keeps_pending(client, conn, fake_llm):
    # I-17：澄清命中但无规范化候选 → 不写、不删 pending，等待后续消息
    _seed_entry(conn, "preference", "style", "active", "正式", "正式", 0.9)
    conn.execute(
        "INSERT INTO pending_clarifications (user_source,user_id,conflicting_ids,question,"
        " issued_at) VALUES ('webchat','u1','[\"preference_style\"]','q',?)", (NOW,))
    conn.commit()
    fake_llm.by_name["clarification_answer_check"] = [{"is_answer": True}]
    fake_llm.by_name["clarify_candidates"] = [{"candidates": []}]
    client.post("/events", json=dict(CONV_EVENT, event_id="evt_keep",
                                     messages=[{"role": "user", "text": "随便吧"}]))
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 1


def test_backup_cleans_stale_tmp(conn, tmp_path):
    # M-4：残留 .tmp 不得阻塞同日重跑；成功落位后不残留 tmp
    target_dir = tmp_path / "bk"
    target_dir.mkdir()
    (target_dir / "backup-20260101.db.tmp").write_text("half-written")
    p = backup(conn, target_dir)
    assert p.exists() and p.stat().st_size > 0
    assert not list(target_dir.glob("*.tmp"))


def test_pending_snapshot_confirm_does_not_override_evolution(client, conn, fake_llm):
    # pending 快照在规范键已被新证据占用时 confirm → 归档，不复活旧猜测（I-2 收尾）
    _seed_entry(conn, "preference", "style", "pending_review", "猜测正式", "猜测", 0.7)
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(
        EXPLICIT_CAND, topic="style", body="明确简洁", summary="简洁",
        quote="记住以后简洁")]}]
    client.post("/events", json=dict(CONV_EVENT, event_id="evt_snap",
                                     messages=[{"role": "user", "text": "记住以后简洁"}]))
    pending_id = conn.execute(
        "SELECT id FROM entries WHERE variant='snapshot'").fetchone()[0]
    item = next(i for i in client.get("/review/queue").json()["items"]
                if i["entry_id"] == pending_id)
    r = client.post(f"/review/webchat/u1/{pending_id}/decision",
                    json={"decision": "confirm", "entry_version": item["entry_version"]})
    assert r.status_code == 200 and r.json()["noop"] is False
    snap = conn.execute(
        "SELECT status FROM entries WHERE id=?", (pending_id,)).fetchone()
    canon = conn.execute(
        "SELECT status, body FROM entries WHERE topic='style' AND variant='normal'").fetchone()
    assert snap["status"] == "archived"
    assert canon["status"] == "active" and canon["body"] == "明确简洁"


def test_settle_pair_never_degrades_taboo_side(conn):
    # 澄清收敛只处理 preference 侧；taboo 恒胜，不得被单侧降级逻辑误伤
    _seed_entry(conn, "taboo", "tool", "active", "不要用 vim", "禁 vim", 0.95)
    _seed_entry(conn, "preference", "tool", "active", "用 vim", "用 vim", 0.9)
    from pismor.pipeline import runner
    rows = conn.execute("SELECT * FROM entries").fetchall()
    runner._settle_pair(conn, "webchat", "u1", rows,
                          {("preference", "tool")}, NOW)
    taboo = conn.execute("SELECT status FROM entries WHERE dimension='taboo'").fetchone()
    assert taboo["status"] == "active"


def test_decision_fix_updates_body_atomically(client, conn):
    entry_id = _seed_entry(conn, "preference", "style", "pending_review",
                           "猜测正式", "猜测", 0.7)
    conn.execute(
        "UPDATE entries SET conflict_with=? WHERE id=?",
        ('["preference_other"]', entry_id))
    conn.commit()
    version = client.get("/review/queue").json()["items"][0]["entry_version"]
    r = client.post("/review/webchat/u1/preference_style/decision",
                    json={"decision": "fix", "entry_version": version,
                          "body": "修正后的陈述", "summary": "修正"})
    assert r.status_code == 200 and r.json()["noop"] is False
    row = conn.execute(
        "SELECT status, body, summary, conflict_with FROM entries WHERE id=?",
        (entry_id,)).fetchone()
    # fix 只修正内容，不自动确认、不改变状态、不清冲突
    assert (row["status"], row["body"], row["summary"]) == (
        "pending_review", "修正后的陈述", "修正")
    assert row["conflict_with"] == '["preference_other"]'


def test_events_from_deleted_user_not_persisted(client, conn, fake_llm):
    """intake 层 tombstone：删除后新事件不再写 entries/sources，也不再调用 LLM。"""
    fake_llm.by_name["extract_candidates"] = [{"candidates": [dict(EXPLICIT_CAND)]}]
    client.post("/events", json=CONV_EVENT)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1
    deleted = client.request("DELETE", "/memory/webchat/u1").json()
    assert deleted["deleted"] is True and deleted["mode"] == "soft"

    calls_before = len(fake_llm.calls)
    r = client.post("/events", json={**CONV_EVENT, "event_id": "evt_after_delete"})
    assert r.status_code == 410
    # 软删除不物理清数据；事件在 intake 前被 tombstone 拦截
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1
    assert len(fake_llm.calls) == calls_before


@pytest.fixture
def writable_settings(tmp_path):
    dst = tmp_path / "taxonomy.yaml"
    shutil.copy(REPO / "config" / "taxonomy.yaml", dst)
    return Settings(
        db_path=tmp_path / "api.db",
        taxonomy_path=dst,
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(enabled=False, endpoint="", model="",
                                    api_key_env="", dimensions=4, batch_size=8))


@pytest.fixture
def writable_client(writable_settings, fake_llm, embedder_stub):
    return TestClient(create_app(writable_settings, llm=fake_llm, embedder=embedder_stub))


def test_review_decision_writes_alias(writable_client, writable_settings):
    tax = load_taxonomy(writable_settings.taxonomy_path)
    canon = tax.dimensions["preference"].topics[0]
    with closing(get_conn(writable_settings.db_path)) as conn:
        eid = _seed_entry(conn, "preference", "手工模型", "pending_review",
                          "b", "s", 0.9, now=NOW)
    r = writable_client.post(
        f"/review/webchat/u1/{eid}/decision",
        json={"decision": "confirm", "entry_version": NOW, "topic": canon})
    assert r.status_code == 200
    assert "_pending_alias" not in r.json()
    raw = yaml.safe_load(writable_settings.taxonomy_path.read_text(encoding="utf-8"))
    pref = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert pref["topic_aliases"]["手工模型"] == canon
    store = writable_client.app.state.taxonomy_store.current()
    assert store.dimensions["preference"].topic_aliases["手工模型"] == canon


def test_alias_write_failure_does_not_fail_review(writable_client, writable_settings,
                                                  monkeypatch):
    monkeypatch.setattr("pismor.core.taxonomy_edit.write_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    tax = load_taxonomy(writable_settings.taxonomy_path)
    canon = tax.dimensions["preference"].topics[0]
    with closing(get_conn(writable_settings.db_path)) as conn:
        eid = _seed_entry(conn, "preference", "手工模型", "pending_review",
                          "b", "s", 0.9, now=NOW)
    r = writable_client.post(
        f"/review/webchat/u1/{eid}/decision",
        json={"decision": "confirm", "entry_version": NOW, "topic": canon})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    raw = yaml.safe_load(writable_settings.taxonomy_path.read_text(encoding="utf-8"))
    pref = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert "手工模型" not in (pref.get("topic_aliases") or {})
    store = writable_client.app.state.taxonomy_store.current()
    assert "手工模型" not in store.dimensions["preference"].topic_aliases
