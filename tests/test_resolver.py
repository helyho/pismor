"""四路决策器测试：ADD/UPDATE/CONFLICT/NOOP 判定 + ORPHAN 透传 + taboo 恒胜复合场景。"""
import pytest

from pismor.pipeline.resolver import resolve


def cand(dimension, topic, scope, body="新候选正文", **kw):
    c = {"dimension": dimension, "topic": topic, "scope": scope, "body": body,
         "summary": "摘要", "confidence": 0.9, "kind": "explicit",
         "quote": "对话原文", "topic_new": None}
    c.update(kw)
    return c


class ScriptedLLM:
    """按脚本回复；记录调用以断言决策 LLM 是否被触发。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat_json(self, system, user, schema):
        self.calls.append((system, user, schema))
        return self.reply


class TabooAwareLLM:
    """taboo 复合场景 fake：按 RESOLVE_USER 的"新候选维度"分流——taboo 侧 UPDATE，其余 CONFLICT。"""

    def __init__(self):
        self.calls = []

    def chat_json(self, system, user, schema):
        self.calls.append((system, user, schema))
        decision = "UPDATE" if "维度 taboo" in user else "CONFLICT"
        return {"decision": decision, "target": [], "reason": decision}


@pytest.fixture
def conn_db(conn):
    return conn


@pytest.fixture
def seeded(conn):
    def _seed(dimension, topic, scope, body="旧条目正文"):
        ts = "2026-11-01T00:00:00+08:00"
        conn.execute(
            "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
            "body,summary,created_at,updated_at,last_reinforced_at)"
            " VALUES ('w','u',?,?,?,?,?,'active',?,'旧摘要',?,?,?)",
            (dimension, topic, scope, f"{dimension}_{topic}_{scope}", 0.8, body, ts, ts, ts))
        conn.commit()
    return _seed


@pytest.fixture
def seeded_taboo_and_pref(conn, seeded):
    seeded("taboo", "style", "global")
    seeded("preference", "style", "global")
    return conn


@pytest.fixture
def fake_llm_add():
    # existing 为 None 时直接 ADD，决策 LLM 不应被调用
    return ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "不应被调用"})


@pytest.fixture
def fake_llm_update():
    return ScriptedLLM({"decision": "UPDATE", "target": ["preference", "lang", "global"], "reason": "演进"})


@pytest.fixture
def fake_llm_conflict():
    return TabooAwareLLM()


def test_add_when_no_existing(conn, fake_llm_add):
    actions = resolve(conn, "w", "u", [cand("preference", "lang", "global")], fake_llm_add)
    assert actions[0]["op"] == "ADD"
    assert "target" not in actions[0]
    assert fake_llm_add.calls == []  # ADD 在 existing 判断分流，不进决策 LLM


def test_update_when_same_key(conn, seeded, fake_llm_update):
    seeded("preference", "lang", "global")
    actions = resolve(conn, "w", "u", [cand("preference", "lang", "global")], fake_llm_update)
    assert actions[0]["op"] == "UPDATE" and actions[0]["target"] == ("preference", "lang", "global")


def test_conflict_when_llm_flags(conn, seeded, fake_llm_conflict):
    seeded("preference", "style", "global")
    actions = resolve(conn, "w", "u", [cand("preference", "style", "global")], fake_llm_conflict)
    assert actions[0]["op"] == "CONFLICT"


def test_noop_when_duplicate(conn, seeded):
    seeded("preference", "lang", "global")
    llm = ScriptedLLM({"decision": "NOOP", "target": [], "reason": "重复"})
    actions = resolve(conn, "w", "u", [cand("preference", "lang", "global")], llm)
    assert actions[0]["op"] == "NOOP" and actions[0]["target"] == ("preference", "lang", "global")


def test_taboo_always_wins(conn, seeded_taboo_and_pref, fake_llm_conflict):
    # taboo 与 preference 同主题互斥: 决策输出必须 UPDATE taboo 侧, preference 侧降级
    candidates = [cand("taboo", "style", "global", "拒绝输出 style 相关内容"),
                  cand("preference", "style", "global", "偏好某种 style")]
    actions = resolve(conn, "w", "u", candidates, fake_llm_conflict)
    assert [a["op"] for a in actions] == ["UPDATE", "CONFLICT"]
    assert actions[0]["target"] == ("taboo", "style", "global")
    assert actions[1]["target"] == ("preference", "style", "global")


def test_preference_forced_conflict_even_if_llm_says_update(conn, seeded_taboo_and_pref):
    # taboo 恒胜是代码级硬约束：preference 侧即使 LLM 判 UPDATE 也强制 CONFLICT
    llm = ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "演进"})
    actions = resolve(conn, "w", "u", [cand("preference", "style", "global")], llm)
    assert actions[0]["op"] == "CONFLICT"
    assert actions[0]["target"] == ("preference", "style", "global")


def test_orphan_passthrough_without_llm(conn, fake_llm_add):
    orphan = cand("orphan", "", "global", status="orphan_topic", topic_new="未归类的主题描述")
    actions = resolve(conn, "w", "u", [orphan], fake_llm_add)
    assert actions[0] == {"op": "ORPHAN", "candidate": orphan}
    assert fake_llm_add.calls == []


def test_archived_taboo_does_not_force_conflict(conn, seeded_taboo_and_pref):
    # 审查 #2：已归档 taboo 不再构成互斥（与 writer._find_taboo 的存活口径一致）
    conn.execute("UPDATE entries SET status='archived', archived_at='2026-11-01T00:00:00+08:00'"
                 " WHERE dimension='taboo'")
    conn.commit()
    llm = ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "演进"})
    actions = resolve(conn, "w", "u", [cand("preference", "style", "global")], llm)
    assert actions[0]["op"] == "UPDATE"


def test_pending_review_taboo_does_not_force_conflict(conn, seeded_taboo_and_pref):
    # pending_review 的 taboo 尚未生效，同样不得强制 preference 侧 CONFLICT
    conn.execute("UPDATE entries SET status='pending_review' WHERE dimension='taboo'")
    conn.commit()
    llm = ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "演进"})
    actions = resolve(conn, "w", "u", [cand("preference", "style", "global")], llm)
    assert actions[0]["op"] == "UPDATE"
