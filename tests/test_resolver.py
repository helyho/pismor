"""四路决策器测试：ADD/UPDATE/CONFLICT/NOOP 判定 + ORPHAN 透传 + taboo 恒胜复合场景。"""
import pytest

from pismor.pipeline.resolver import (
    LlmUnavailable,
    neighbor_candidates,
    neighbor_conflict,
    resolve,
)


def cand(dimension, topic, body="新候选正文", **kw):
    c = {"dimension": dimension, "topic": topic, "body": body,
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
    def _seed(dimension, topic, body="旧条目正文", *, variant="normal", status="active",
              degraded_reason=None, archived_at=None):
        ts = "2026-11-01T00:00:00+08:00"
        entry_id = f"{dimension}_{topic}_{variant}"
        conn.execute(
            "INSERT INTO entries (user_source,user_id,dimension,topic,id,variant,variant_seq,"
            "confidence,status,body,summary,created_at,updated_at,last_reinforced_at,"
            "degraded_reason,archived_at)"
            " VALUES ('w','u',?,?,?,?,0,0.8,?,?,'旧摘要',?,?,?,?,?)",
            (dimension, topic, entry_id, variant, status, body, ts, ts, ts,
             degraded_reason, archived_at))
        conn.commit()
        return entry_id
    return _seed


@pytest.fixture
def seeded_taboo_and_pref(conn, seeded):
    seeded("taboo", "style")
    seeded("preference", "style")
    return conn


@pytest.fixture
def fake_llm_add():
    # existing 为 None 时直接 ADD，决策 LLM 不应被调用
    return ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "不应被调用"})


@pytest.fixture
def fake_llm_update():
    return ScriptedLLM({"decision": "UPDATE", "target": ["preference", "lang"], "reason": "演进"})


@pytest.fixture
def fake_llm_conflict():
    return TabooAwareLLM()


def test_add_when_no_existing(conn, fake_llm_add):
    actions = resolve(conn, "w", "u", [cand("preference", "lang")], fake_llm_add)
    assert actions[0]["op"] == "ADD"
    assert "target" not in actions[0]
    assert fake_llm_add.calls == []  # ADD 在 existing 判断分流，不进决策 LLM


def test_update_when_same_key(conn, seeded, fake_llm_update):
    seeded("preference", "lang")
    actions = resolve(conn, "w", "u", [cand("preference", "lang")], fake_llm_update)
    assert actions[0]["op"] == "UPDATE" and actions[0]["target"] == ("preference", "lang")


def test_conflict_when_llm_flags(conn, seeded, fake_llm_conflict):
    seeded("preference", "style")
    actions = resolve(conn, "w", "u", [cand("preference", "style")], fake_llm_conflict)
    assert actions[0]["op"] == "CONFLICT"


def test_noop_when_duplicate(conn, seeded):
    seeded("preference", "lang")
    llm = ScriptedLLM({"decision": "NOOP", "target": [], "reason": "重复"})
    actions = resolve(conn, "w", "u", [cand("preference", "lang")], llm)
    assert actions[0]["op"] == "NOOP" and actions[0]["target"] == ("preference", "lang")


def test_taboo_always_wins(conn, seeded_taboo_and_pref, fake_llm_conflict):
    # taboo 与 preference 同主题互斥: 决策输出必须 UPDATE taboo 侧, preference 侧降级
    candidates = [cand("taboo", "style", "拒绝输出 style 相关内容"),
                  cand("preference", "style", "偏好某种 style")]
    actions = resolve(conn, "w", "u", candidates, fake_llm_conflict)
    assert [a["op"] for a in actions] == ["UPDATE", "CONFLICT"]
    assert actions[0]["target"] == ("taboo", "style")
    assert actions[1]["target"] == ("preference", "style")


def test_preference_forced_conflict_even_if_llm_says_update(conn, seeded_taboo_and_pref):
    # taboo 恒胜是代码级硬约束：preference 侧即使 LLM 判 UPDATE 也强制 CONFLICT
    llm = ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "演进"})
    actions = resolve(conn, "w", "u", [cand("preference", "style")], llm)
    assert actions[0]["op"] == "CONFLICT"
    assert actions[0]["target"] == ("preference", "style")


def test_orphan_passthrough_without_llm(conn, fake_llm_add):
    orphan = cand("orphan", "", status="orphan_topic", topic_new="未归类的主题描述")
    actions = resolve(conn, "w", "u", [orphan], fake_llm_add)
    assert actions[0] == {"op": "ORPHAN", "candidate": orphan}
    assert fake_llm_add.calls == []


def test_archived_taboo_does_not_force_conflict(conn, seeded_taboo_and_pref):
    # 审查 #2：已归档 taboo 不再构成互斥（与 writer._find_taboo 的存活口径一致）
    conn.execute("UPDATE entries SET status='archived', archived_at='2026-11-01T00:00:00+08:00'"
                 " WHERE dimension='taboo'")
    conn.commit()
    llm = ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "演进"})
    actions = resolve(conn, "w", "u", [cand("preference", "style")], llm)
    assert actions[0]["op"] == "UPDATE"


def test_pending_review_taboo_does_not_force_conflict(conn, seeded_taboo_and_pref):
    # pending_review 的 taboo 尚未生效，同样不得强制 preference 侧 CONFLICT
    conn.execute("UPDATE entries SET status='pending_review' WHERE dimension='taboo'")
    conn.commit()
    llm = ScriptedLLM({"decision": "UPDATE", "target": [], "reason": "演进"})
    actions = resolve(conn, "w", "u", [cand("preference", "style")], llm)
    assert actions[0]["op"] == "UPDATE"


def test_neighbor_conflict_hallucinated_id_falls_back_to_add(conn, seeded):
    # F4: LLM 指认的 conflicting_with 不匹配任何近邻时是幻觉——
    # 旧实现回退首行，会把无关 active 行拖进矛盾双存并降级×0.8；改为宁当轮 ADD
    seeded("taboo", "style")
    llm = ScriptedLLM({"conflict": True, "conflicting_with": "hallucinated_id"})
    actions = resolve(conn, "w", "u", [cand("preference", "color")], llm)
    assert actions[0]["op"] == "ADD"


def test_neighbor_conflict_with_matching_id(conn, seeded):
    # 近邻路径回归：指认匹配现存近邻 id → 照常 CONFLICT
    sid = seeded("taboo", "style")
    llm = ScriptedLLM({"conflict": True, "conflicting_with": sid})
    actions = resolve(conn, "w", "u", [cand("preference", "color")], llm)
    assert actions[0]["op"] == "CONFLICT"


class ExplodingLLM:
    """LLM 调用必抛错的 fake：验证 strict 模式把异常包装为 LlmUnavailable。"""

    def chat_json(self, system, user, schema):
        raise RuntimeError("llm 服务不可用")


def test_neighbor_candidates_queries_peer_dim(conn, taxonomy, seeded):
    # 候选查询抽出后的过滤口径：对端维度 active 规范行命中；archived / 带前缀变形不命中
    seeded("taboo", "style")
    seeded("taboo", "gone")           # 将改为 archived
    seeded("taboo", "tilde_deform", variant="trace", status="archived",
           degraded_reason="superseded", archived_at="2026-11-01T00:00:00+08:00")
    seeded("taboo", "plus_deform", variant="conflict")
    seeded("taboo", "bang_deform", variant="snapshot", status="pending_review")
    conn.execute("UPDATE entries SET status='archived',"
                 " archived_at='2026-11-01T00:00:00+08:00' WHERE topic='gone'")
    conn.commit()
    rows = neighbor_candidates(conn, "w", "u", "preference")
    assert [(r["dimension"], r["topic"]) for r in rows] == [("taboo", "style")]
    # 反向配对：查 taboo 命中 preference 规范行
    seeded("preference", "tone")
    rows = neighbor_candidates(conn, "w", "u", "taboo")
    assert [(r["dimension"], r["topic"]) for r in rows] == [("preference", "tone")]
    # LIMIT _NEIGHBOR_LIMIT(5)：6 行规范行只取前 5
    for i in range(6):
        seeded("taboo", f"bulk_{i}")
    assert len(neighbor_candidates(conn, "w", "u", "preference")) == 5


def test_strict_raises_when_llm_missing(conn, seeded):
    # admin 手工录入路径（strict=True）：有候选行但 LLM 缺失 → 拒绝而非静默跳过
    seeded("taboo", "style")
    with pytest.raises(LlmUnavailable):
        neighbor_conflict(conn, "w", "u", cand("preference", "color"), None, strict=True)


def test_non_strict_returns_none_when_llm_missing(conn, seeded):
    # 管线现状不变：llm 缺失时静默跳过近邻检测（容错），返回 None 走 ADD
    seeded("taboo", "style")
    assert neighbor_conflict(conn, "w", "u", cand("preference", "color"), None) is None


def test_strict_raises_on_llm_error(conn, seeded):
    # strict=True：LLM 调用抛任何异常都包装为 LlmUnavailable（拒绝创建）
    seeded("taboo", "style")
    with pytest.raises(LlmUnavailable):
        neighbor_conflict(conn, "w", "u", cand("preference", "color"), ExplodingLLM(),
                          strict=True)


def test_no_candidates_skips_llm_even_strict(conn):
    # 无候选行时 LLM 根本不该被调用——strict=True 也不抛（无需求即无不可用）
    llm = ScriptedLLM({"conflict": True, "conflicting_with": "x", "reason": "不应被调用"})
    assert neighbor_conflict(conn, "w", "u", cand("preference", "color"), llm,
                             strict=True) is None
    assert llm.calls == []
