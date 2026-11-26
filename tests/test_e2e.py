"""Task 14 端到端集成测试：真实 API 全链路（ScriptedLLM 按 schema/调用序供给）。

覆盖 spec 验收点：幂等重复 200、budget 截断、degraded 前缀、taboo 恒第一、
澄清闭环、导出确定性、tombstone 空包，以及 conversation/text 双通道。
"""
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pismor.serving.api import create_app
from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.core.db import get_conn, init_schema

REPO = Path(__file__).resolve().parent.parent
NOW = "2026-11-24T10:00:00+08:00"


def S(name, response):
    """带 schema 顺序自检的脚本条目。"""
    return {"schema": name, "response": response}


def conv(event_id, text, *, source="webchat", user="u1", session="s1", turn=1,
         ts=NOW, force_extract=False):
    return {"source": source, "user_ref": user, "event_id": event_id,
            "event_type": "conversation", "session_id": session, "turn": turn,
            "ts": ts, "messages": [{"role": "user", "text": text},
                                   {"role": "assistant", "text": "好的"}],
            "force_extract": force_extract}


def text_event(event_id, content, *, source="webchat", user="u1", ts=NOW):
    return {"source": source, "user_ref": user, "event_id": event_id,
            "event_type": "text", "content": content, "ts": ts,
            "origin_ref": f"doc:{event_id}"}


def cand(dimension, topic, body, summary, quote, *, confidence=0.9, kind="explicit",
         scope="global"):
    return {"dimension": dimension, "topic": topic, "scope": scope, "body": body,
            "summary": summary, "confidence": confidence, "kind": kind,
            "quote": quote, "topic_new": None}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "e2e.db",
        taxonomy_path=REPO / "config" / "taxonomy.yaml",
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(enabled=False, endpoint="", model="",
                                    api_key_env="", dimensions=4, batch_size=8),
    )


@pytest.fixture
def client_with_script(settings, fake_llm_scripted):
    def _make(script=()):
        llm = fake_llm_scripted(list(script))
        app = create_app(settings, llm=llm, embedder=None)
        return TestClient(app), llm
    return _make


@pytest.fixture
def db(settings):
    conn = get_conn(settings.db_path)
    init_schema(conn, None)
    yield conn
    conn.close()


def _post(client, llm, event):
    resp = client.post("/events", json=event)
    # TestClient 同步执行 BackgroundTasks：返回时管线已跑完；脚本错位在此立刻暴露
    assert llm.errors == [], llm.errors
    return resp


def _assert_consumed(llm):
    assert llm.errors == [], llm.errors
    assert llm.remaining == [], llm.remaining


def _entry(db, dim, topic, *, us="webchat", uid="u1", scope=None):
    if scope is None:
        return db.execute(
            "SELECT * FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
            " ORDER BY scope", (us, uid, dim, topic)).fetchone()
    return db.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
        " AND scope=?", (us, uid, dim, topic, scope)).fetchone()


def _seed(db, dim, topic, body, *, status="active", conf=0.9, scope="global",
          us="webchat", uid="u1", degraded_reason=None, conflict_with=None):
    entry_id = f"{dim}_{topic}@{scope}"
    db.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        " body,summary,created_at,updated_at,last_reinforced_at,reinforcements,"
        " degraded_reason,conflict_with)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (us, uid, dim, topic, scope, entry_id, conf, status, body, body[:20],
         NOW, NOW, NOW, degraded_reason, conflict_with))
    db.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (entry_id, us, uid, "conversation", "seed", f"quote-{topic}", "explicit"))
    db.commit()
    return entry_id


def test_full_lifecycle(client_with_script, db):
    """偏好产生 → 矛盾 → 澄清挂起 → 回答闭环 → text 通道 → 导出文件集。"""
    pref_v1 = cand("preference", "style", "回复使用正式书面语", "正式书面语",
                   "以后回复都用正式书面语", confidence=0.95)
    pref_v2 = cand("preference", "style", "聊天回复随意自然", "随意自然",
                   "以后聊天回复随意一点，别太正式", confidence=0.9)
    fact_note = cand("fact", "event", "写过一篇 Rust 所有权机制笔记", "Rust 笔记",
                     "今天写了一篇关于 Rust 所有权机制的笔记")
    taboo_cand = cand("taboo", "output_format", "不要使用 emoji 表情", "禁用 emoji",
                      "别再给我用 emoji 表情了", confidence=0.95)
    identity_cand = cand("identity", "name", "用户名字是张三", "名字张三",
                         "记住我叫张三，是后端工程师", confidence=0.95)
    answer = "平时聊天随意点，写东西的时候再正式"

    script = [
        S("extract_candidates", {"candidates": [pref_v1]}),
        S("extract_candidates", {"candidates": [pref_v2]}),
        S("resolve_decision", {"decision": "CONFLICT", "reason": "正式与随意互斥"}),
        S("clarification_answer_check", {"is_answer": True}),
        S("clarify_candidates", {"candidates": [
            cand("preference", "style", "日常聊天回复随意自然", "聊天随意自然", answer,
                 confidence=0.95),
            cand("preference", "style", "写作场景回复使用正式书面语", "写作正式", answer,
                 confidence=0.95, scope="project:writing"),
        ]}),
        S("extract_candidates", {"candidates": [fact_note]}),
        S("extract_candidates", {"candidates": [taboo_cand]}),
        # 第八轮：taboo ADD 前近邻互斥检测（库内 preference_style@global 存活 → 查一次，判不互斥）
        S("neighbor_conflict", {"conflict": False, "reason": "emoji 禁忌与语气偏好无关"}),
        S("extract_candidates", {"candidates": [identity_cand]}),
    ]
    client, llm = client_with_script(script)

    # 1) 对话产生偏好（explicit 直通抽取，无同 key → ADD 不调决策 LLM）
    assert _post(client, llm, conv("evt_1", "以后回复都用正式书面语")).status_code == 202
    row = _entry(db, "preference", "style")
    assert row["status"] == "active" and row["body"] == "回复使用正式书面语"
    assert llm.count("resolve_decision") == 0

    # 2) 矛盾出现 → CONFLICT 双存；装配挂澄清问题（模板拼接不调 LLM）
    assert _post(client, llm, conv("evt_2", "以后聊天回复随意一点，别太正式",
                                   turn=2)).status_code == 202
    plus = _entry(db, "preference", "+style")
    degraded_main = _entry(db, "preference", "style")
    assert degraded_main["conflict_with"] and plus["status"] == "degraded" \
        and plus["degraded_reason"] == "conflict_lost"
    assert json.loads(degraded_main["conflict_with"]) == ["preference_+style@global"]

    pack = client.get("/profile/webchat/u1?budget=2000").json()
    assert len(pack["clarification_needed"]) == 1
    item = pack["clarification_needed"][0]
    assert item["conflicting_ids"] == ["preference_style@global", "preference_+style@global"]
    assert "正式书面语" in item["question"] and "随意自然" in item["question"]
    assert db.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 1

    # 3) 用户回答 → LLM 规范化 → scope 拆分（global 随意 + project:writing 正式）
    assert _post(client, llm, conv("evt_3", answer, turn=3)).status_code == 202
    main = _entry(db, "preference", "style", scope="global")
    opposed = _entry(db, "preference", "+style", scope="global")
    scoped = _entry(db, "preference", "style", scope="project:writing")
    # 原文祈使句不入库：body 是抽取出的陈述句（C-2）
    assert main["status"] == "active" and main["body"] == "日常聊天回复随意自然"
    assert main["body"] != answer and main["summary"] == "聊天随意自然"
    assert main["conflict_with"] is None and opposed["conflict_with"] is None
    assert scoped is not None and scoped["status"] == "active"
    assert scoped["body"] == "写作场景回复使用正式书面语"
    # '+' 变形行按裁决归档为轨迹，不得原样 active（T14）
    assert opposed["status"] == "archived" and opposed["archived_at"] == NOW
    assert db.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 0
    kinds = [r["kind"] for r in db.execute(
        "SELECT kind FROM sources WHERE entry_id='preference_style@global'").fetchall()]
    assert "clarification" in kinds
    assert client.get("/profile/webchat/u1?budget=2000").json()["clarification_needed"] == []

    # 4) text 事件独立通道：直通抽取，不消费澄清判定
    assert _post(client, llm, text_event("evt_4", "今天写了一篇关于 Rust 所有权机制的笔记")
                 ).status_code == 202
    note = _entry(db, "fact", "event")
    assert note["status"] == "active"
    src = db.execute("SELECT origin_type FROM sources WHERE entry_id='fact_event@global'"
                     ).fetchone()
    assert src["origin_type"] == "text"

    # 5) taboo / identity 经管线产生
    assert _post(client, llm, conv("evt_5", "别再给我用 emoji 表情了", turn=1)).status_code == 202
    assert _post(client, llm, conv("evt_6", "记住我叫张三，是后端工程师", turn=1)).status_code == 202
    _assert_consumed(llm)
    assert llm.count("extract_candidates") == 5
    assert llm.count("clarify_candidates") == 1
    assert llm.count("resolve_decision") == 1
    assert llm.count("clarification_answer_check") == 1


def test_repeated_get_keeps_clarification(client_with_script, db):
    # ① 同一秒内重复装配：issue 去重复用问题，不再吞空/累积 pending 行
    client, _llm = client_with_script([])
    _seed(db, "preference", "style", "回复使用正式书面语", conf=0.76, status="degraded",
          degraded_reason="conflict_lost", conflict_with='["preference_+style"]')
    _seed(db, "preference", "+style", "聊天回复随意自然", conf=0.72, status="degraded",
          degraded_reason="conflict_lost", conflict_with='["preference_style"]')

    first = client.get("/profile/webchat/u1?budget=2000").json()["clarification_needed"]
    second = client.get("/profile/webchat/u1?budget=2000").json()["clarification_needed"]
    assert len(first) == 1 and second == first
    assert db.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 1


def test_clarification_updates_canonical_main_row(client_with_script, db):
    # ② 新证据置信更高（0.95 > 旧行 0.90）→ conflicting_ids 首项是 '+style' 变形行，
    # 回答仍必须写规范主行 preference_style，变形行只清冲突标记
    pref_v1 = cand("preference", "style", "回复使用正式书面语", "正式书面语",
                   "以后回复都用正式书面语", confidence=0.9)
    pref_v2 = cand("preference", "style", "聊天回复随意自然", "随意自然",
                   "以后聊天回复随意一点，别太正式", confidence=0.95)
    answer = "平时聊天随意点，写东西的时候再正式"
    client, llm = client_with_script([
        S("extract_candidates", {"candidates": [pref_v1]}),
        S("extract_candidates", {"candidates": [pref_v2]}),
        S("resolve_decision", {"decision": "CONFLICT", "reason": "正式与随意互斥"}),
        S("clarification_answer_check", {"is_answer": True}),
        S("clarify_candidates", {"candidates": [
            cand("preference", "style", "回复保持正式书面语", "正式书面语", answer,
                 confidence=0.95)]}),
    ])

    _post(client, llm, conv("evt_h1", "以后回复都用正式书面语"))
    _post(client, llm, conv("evt_h2", "以后聊天回复随意一点，别太正式", turn=2))
    pack = client.get("/profile/webchat/u1?budget=2000").json()
    assert pack["clarification_needed"][0]["conflicting_ids"] == \
        ["preference_+style@global", "preference_style@global"]  # 高置信变形行在首项

    _post(client, llm, conv("evt_h3", answer, turn=3))
    _assert_consumed(llm)
    main = _entry(db, "preference", "style")
    plus = _entry(db, "preference", "+style")
    assert main["status"] == "active" and main["body"] == "回复保持正式书面语"
    assert main["body"] != answer and len(main["summary"]) <= 20
    assert main["conflict_with"] is None and plus["conflict_with"] is None
    assert plus["status"] == "archived"  # '+' 行归档，不复活为 active
    assert db.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 0
    assert client.get("/profile/webchat/u1?budget=2000").json()["clarification_needed"] == []


def test_idempotent_duplicate_200(client_with_script, db):
    client, llm = client_with_script([
        S("extract_candidates", {"candidates": [
            cand("preference", "lang", "代码主语言 Python", "主语言Python", "以后都用 Python")]}),
    ])
    event = conv("evt_dup", "以后都用 Python")
    assert _post(client, llm, event).status_code == 202
    resp = client.post("/events", json=event)
    assert resp.status_code == 200 and resp.json() == {"deduplicated": True}
    assert llm.count("extract_candidates") == 1  # 重放不再触发管线
    assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1


def test_text_event_skips_clarification_channel(client_with_script, db):
    client, llm = client_with_script([
        S("extract_candidates", {"candidates": [
            cand("fact", "event", "写过 Rust 笔记", "Rust 笔记", "今天写了 Rust 笔记")]}),
    ])
    issued = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT INTO pending_clarifications (user_source,user_id,conflicting_ids,"
               "question,issued_at) VALUES ('webchat','u1','[\"preference_style\"]','q',?)",
               (issued,))
    db.commit()
    assert _post(client, llm, text_event("evt_text", "今天写了 Rust 笔记")).status_code == 202
    assert llm.count("clarification_answer_check") == 0
    assert db.execute("SELECT COUNT(*) FROM pending_clarifications").fetchone()[0] == 1
    assert _entry(db, "fact", "event")["status"] == "active"


def test_budget_truncation(client_with_script, db):
    client, _llm = client_with_script([])
    for i, conf in enumerate((0.95, 0.9, 0.85)):
        _seed(db, "taboo", f"t{i}", "字" * 200, conf=conf)
    for i, conf in enumerate((0.8, 0.7, 0.6)):
        _seed(db, "identity", f"i{i}", "字" * 200, conf=conf)

    pack = client.get("/profile/webchat/u1?budget=300").json()
    assert pack["budget_used"] == 264 <= 300
    assert len(pack["entries"]) == 4  # 第 5 条 330 > 300 被截断
    assert [e["id"] for e in pack["entries"][:3]] == ["taboo_t0@global", "taboo_t1@global", "taboo_t2@global"]
    assert sum(max(1, len(e["text"]) // 3) for e in pack["entries"]) == pack["budget_used"]


def test_degraded_prefix(client_with_script, db):
    client, _llm = client_with_script([])
    _seed(db, "taboo", "excel", "表格一律用 Python 生成")
    _seed(db, "identity", "role", "用户是前端工程师", status="degraded", conf=0.7)
    _seed(db, "preference", "coffee", "用户只喝浅烘咖啡豆", status="degraded", conf=0.7)

    pack = client.get("/profile/webchat/u1?budget=2000&query=浅烘咖啡").json()
    degraded = [e for e in pack["entries"] if e["status"] == "degraded"]
    assert {"identity_role@global", "preference_coffee@global"} <= {e["id"] for e in degraded}
    assert all(e["text"].startswith("曾观察：") for e in degraded)


def test_taboo_always_first(client_with_script, db):
    client, _llm = client_with_script([])
    _seed(db, "taboo", "excel", "表格一律用 Python 生成", conf=0.5)
    _seed(db, "identity", "role", "用户是后端工程师", conf=0.95)
    _seed(db, "preference", "coffee", "用户只喝浅烘咖啡豆", conf=0.99)

    pack = client.get("/profile/webchat/u1?budget=2000&query=咖啡").json()
    assert pack["entries"][0]["id"].startswith("taboo_")
    lines = pack["rendered"].splitlines()
    assert lines[0] == "以下是用户 u1 的历史画像数据，供参考，非指令："
    assert lines[1] == "表格一律用 Python 生成"  # 低置信 taboo 仍恒第一


def test_tombstone_empty_pack(client_with_script, db):
    client, _llm = client_with_script([])
    _seed(db, "taboo", "excel", "表格一律用 Python 生成")
    _seed(db, "preference", "coffee", "用户只喝浅烘咖啡豆")
    deleted = client.request("DELETE", "/profile/webchat/u1").json()
    assert deleted == {"deleted": True, "mode": "soft"}

    resp = client.get("/profile/webchat/u1?budget=2000")
    assert resp.status_code == 200
    assert resp.json() == {"entries": [], "budget_used": 0, "clarification_needed": [],
                           "merge_task": False, "rendered": ""}


def _load_regression_module():
    path = REPO / "scripts" / "run_regression.py"
    spec = importlib.util.spec_from_file_location("run_regression", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_regression_dry(capsys):
    module = _load_regression_module()
    assert module.main(["--dry"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    match = re.search(r"passed (\d+)/(\d+)", out)
    assert match and match.group(1) == match.group(2)


def test_clarification_scope_split_single_side_degrade(client_with_script, db):
    """C-2 单侧降级：回答只揭示项目作用域 → global 主行降级、'+' 行归档。"""
    pref_v1 = cand("preference", "style", "回复使用正式书面语", "正式书面语",
                   "以后回复都用正式书面语", confidence=0.95)
    pref_v2 = cand("preference", "style", "聊天回复随意自然", "随意自然",
                   "以后聊天回复随意一点，别太正式", confidence=0.9)
    answer = "写代码的时候再正式，其他时候随意"
    script = [
        S("extract_candidates", {"candidates": [pref_v1]}),
        S("extract_candidates", {"candidates": [pref_v2]}),
        S("resolve_decision", {"decision": "CONFLICT", "reason": "互斥"}),
        S("clarification_answer_check", {"is_answer": True}),
        S("clarify_candidates", {"candidates": [
            cand("preference", "style", "写代码场景回复使用正式书面语", "代码正式", answer,
                 confidence=0.95, scope="project:code")]}),
    ]
    client, llm = client_with_script(script)
    _post(client, llm, conv("evt_s1", "以后回复都用正式书面语"))
    _post(client, llm, conv("evt_s2", "以后聊天回复随意一点，别太正式", turn=2))
    assert client.get("/profile/webchat/u1?budget=2000").json()["clarification_needed"]
    _post(client, llm, conv("evt_s3", answer, turn=3))
    _assert_consumed(llm)
    main = _entry(db, "preference", "style", scope="global")
    plus = _entry(db, "preference", "+style", scope="global")
    scoped = _entry(db, "preference", "style", scope="project:code")
    assert main["status"] == "degraded" and main["degraded_reason"] == "conflict_lost"
    assert plus["status"] == "archived"
    assert scoped["status"] == "active" and scoped["body"] == "写代码场景回复使用正式书面语"


def test_clarification_targets_scope_in_pending_pair(client_with_script, db):
    """C-1：pending 指向 project:x 时，澄清只改该 scope 的规范行，global 行不动。"""
    _seed(db, "preference", "style", "全局正式", scope="global")
    _seed(db, "preference", "style", "项目正式", scope="project:x",
          conflict_with='["preference_+style@project:x"]')
    _seed(db, "preference", "+style", "项目随意", scope="project:x", status="degraded",
          degraded_reason="conflict_lost", conflict_with='["preference_style@project:x"]')
    db.execute("INSERT INTO pending_clarifications (user_source,user_id,conflicting_ids,"
               "question,issued_at) VALUES ('webchat','u1',?, 'q', ?)",
               ('["preference_style@project:x","preference_+style@project:x"]', NOW))
    db.commit()
    script = [
        S("clarification_answer_check", {"is_answer": True}),
        S("clarify_candidates", {"candidates": [
            cand("preference", "style", "项目内回复使用口语", "项目口语", "项目里随意说",
                 confidence=0.95, scope="project:x")]}),
    ]
    client, llm = client_with_script(script)
    _post(client, llm, conv("evt_x1", "项目里随意说"))
    _assert_consumed(llm)
    global_row = db.execute(
        "SELECT body, status FROM entries WHERE scope='global' AND topic='style'").fetchone()
    project_row = db.execute(
        "SELECT body, status FROM entries WHERE scope='project:x' AND topic='style'").fetchone()
    assert global_row["body"] == "全局正式" and global_row["status"] == "active"
    assert project_row["body"] == "项目内回复使用口语" and project_row["status"] == "active"
    assert db.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 0
