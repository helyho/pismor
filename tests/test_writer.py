"""Task 7 写入器测试：brief 测试 + 裁决钉死（taboo 恒胜/轨迹行/双存变形/向量降级）。"""
import json

import pytest

from mem.embedder import EmbedUnavailable
from mem.writer import IllegalTransition, apply_actions

NOW = "2026-11-24T00:00:00+08:00"


def full_cand(dimension, topic, scope, body="新值", quote="用户原话"):
    # candidate 键见 extractor：dimension/topic/scope/body/summary/confidence/kind/quote/topic_new
    return {"dimension": dimension, "topic": topic, "scope": scope,
            "body": body, "summary": body[:20], "confidence": 0.9,
            "kind": "explicit", "quote": quote, "topic_new": None}


def add_action(dimension="preference", topic="lang", scope="global"):
    return {"op": "ADD", "candidate": full_cand(dimension, topic, scope, body="首见")}


def update_action(dimension, topic, scope):
    return {"op": "UPDATE", "candidate": full_cand(dimension, topic, scope),
            "target": (dimension, topic, scope)}


def conflict_action(dimension, topic, scope):
    return {"op": "CONFLICT", "candidate": full_cand(dimension, topic, scope, body="相反表述"),
            "target": (dimension, topic, scope)}


def noop_action(dimension, topic, scope):
    return {"op": "NOOP", "candidate": full_cand(dimension, topic, scope),
            "target": (dimension, topic, scope)}


def orphan_action():
    c = full_cand("orphan", "种植多肉", "global")
    c["status"] = "orphan_topic"
    c["topic_new"] = "用户谈多肉植物养护"
    return {"op": "ORPHAN", "candidate": c}


class EmbedderStub:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class EmbedderDown:
    def embed(self, texts):
        raise EmbedUnavailable("down")


class EmbedderKeyError:
    def embed(self, texts):
        raise KeyError("data")  # 网关 200 + 异常体路径


@pytest.fixture
def embedder_stub():
    return EmbedderStub()


@pytest.fixture
def embed_down():
    return EmbedderDown()


def _seed(conn, dimension="preference", status="active", reinforcements=2):
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements,archived_at)"
        " VALUES ('w','u',?,'lang','global',?,'0.9',?,'旧值','旧值',"
        "'2026-01-01','2026-01-01','2026-01-01',?,?)",
        (dimension, f"{dimension}_lang", status, reinforcements,
         NOW if status == "archived" else None))
    conn.commit()


@pytest.fixture
def seeded_active(conn):
    _seed(conn)
    return {"reinforcements": 2}


@pytest.fixture
def seeded_archived(conn):
    _seed(conn, status="archived")


def test_add_writes_entry_and_sources(conn, embedder_stub):
    apply_actions(conn, "w", "u", [add_action()], embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT * FROM entries").fetchone()
    assert row["status"] == "active" and row["id"] == "preference_lang"
    assert row["last_reinforced_at"] == NOW and row["reinforcements"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1
    src = conn.execute("SELECT * FROM sources").fetchone()
    assert (src["entry_id"], src["origin_type"], src["origin_ref"], src["kind"]) == \
        ("preference_lang", "conversation", "s1:1", "explicit")


def test_update_archives_old(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    # 轨迹行（topic 加 "~" 前缀）：旧行完整内容 archived，superseded_by 指向新行 id
    sup = conn.execute("SELECT * FROM entries WHERE topic='~lang'").fetchone()
    assert sup["status"] == "archived" and sup["superseded_by"] == "preference_lang"
    assert sup["archived_at"] == NOW and sup["body"] == "旧值"
    # 原行同 key 覆盖为新值 active（bi-temporal 旧值由轨迹行承载）
    cur = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert cur["status"] == "active" and cur["body"] == "新值"
    assert cur["last_reinforced_at"] == NOW and cur["superseded_by"] is None


def test_conflict_symmetric(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [conflict_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    old = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    new = conn.execute("SELECT * FROM entries WHERE topic='+lang'").fetchone()
    # Review Focus #4：对称互标
    assert new["id"] in json.loads(old["conflict_with"])
    assert old["id"] in json.loads(new["conflict_with"])
    assert old["confidence"] < 0.85 and new["confidence"] < 0.85
    assert old["status"] == "degraded" and old["degraded_reason"] == "conflict_lost"
    assert new["status"] == "degraded" and new["degraded_reason"] == "conflict_lost"


def test_noop_reinforces(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [noop_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    row = conn.execute("SELECT reinforcements, last_reinforced_at FROM entries").fetchone()
    assert row["reinforcements"] == 3 and row["last_reinforced_at"] == NOW


def test_embed_down_still_writes(conn, embed_down):
    apply_actions(conn, "w", "u", [add_action()], embed_down, "text", "e1", NOW)  # Review Focus #2
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1


def test_embed_keyerror_still_writes(conn):
    # 上游裁决 c：网关 200 + 异常体 → KeyError 同样降级，不抛出
    apply_actions(conn, "w", "u", [add_action()], EmbedderKeyError(), "text", "e1", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1


def test_embed_written_when_table_exists(conn, embedder_stub):
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    apply_actions(conn, "w", "u", [add_action()], embedder_stub, "text", "e1", NOW)
    assert conn.execute("SELECT entry_id FROM embeddings").fetchone()["entry_id"] == "preference_lang"


def test_update_refreshes_vector(conn, embedder_stub):
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    apply_actions(conn, "w", "u", [add_action()], embedder_stub, "text", "e1", NOW)
    apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")],
                  embedder_stub, "text", "e2", NOW)
    rows = conn.execute("SELECT entry_id FROM embeddings").fetchall()
    assert [r["entry_id"] for r in rows] == ["preference_lang"]  # 同 id 覆盖，不重复


def test_orphan_goes_pending(conn, embedder_stub):
    apply_actions(conn, "w", "u", [orphan_action()], embedder_stub, "text", "e1", NOW)
    row = conn.execute("SELECT status, topic_new FROM entries").fetchone()
    assert row["status"] == "orphan_topic" and row["topic_new"]


def test_orphan_recur_reinforces(conn, embedder_stub):
    # 同主题 orphan 复现：计数 +1（§8.2 晋升门槛依据），不重复插行
    apply_actions(conn, "w", "u", [orphan_action()], embedder_stub, "text", "e1", NOW)
    apply_actions(conn, "w", "u", [orphan_action()], embedder_stub, "text", "e2", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1
    assert conn.execute("SELECT reinforcements FROM entries").fetchone()["reinforcements"] == 1


def test_illegal_transition_rejected(conn, seeded_archived, embedder_stub):
    with pytest.raises(IllegalTransition):
        apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")],
                      embedder_stub, "text", "e1", NOW)


def test_illegal_transition_rolls_back_batch(conn, seeded_active, embedder_stub):
    # 整批原子性：合法 NOOP + 非法 UPDATE 同批，抛出后 NOOP 强化也不落库
    actions = [noop_action("preference", "lang", "global"),
               update_action("preference", "lang", "global")]
    actions[1]["target"] = ("preference", "ghost", "global")  # 目标不存在
    with pytest.raises(IllegalTransition):
        apply_actions(conn, "w", "u", actions, embedder_stub, "text", "e1", NOW)
    assert conn.execute("SELECT reinforcements FROM entries").fetchone()["reinforcements"] == 2


def test_batch_taboo_wins_over_preference(conn, embedder_stub):
    # 上游裁决 a：同批双新增乱序给出，写入器内 taboo 先落库，preference 侧恒胜降级
    actions = [add_action("preference", "lang", "global"), add_action("taboo", "lang", "global")]
    apply_actions(conn, "w", "u", actions, embedder_stub, "conversation", "s1:1", NOW)
    pref = conn.execute("SELECT * FROM entries WHERE dimension='preference'").fetchone()
    taboo = conn.execute("SELECT * FROM entries WHERE dimension='taboo'").fetchone()
    assert pref["status"] == "degraded" and pref["degraded_reason"] == "conflict_lost"
    assert taboo["id"] in json.loads(pref["conflict_with"])
    assert pref["id"] in json.loads(taboo["conflict_with"])
    assert taboo["status"] == "active" and taboo["confidence"] == 0.9  # taboo 不降置信不降状态


def test_conflict_with_taboo_taboo_unmoved(conn, embedder_stub):
    # 上游裁决 6：CONFLICT 对方是 taboo → preference 侧 degraded，taboo 只补 conflict_with
    _seed(conn, dimension="taboo")
    _seed(conn)
    apply_actions(conn, "w", "u", [conflict_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    taboo = conn.execute("SELECT * FROM entries WHERE dimension='taboo'").fetchone()
    pref_old = conn.execute("SELECT * FROM entries WHERE dimension='preference' AND topic='lang'").fetchone()
    pref_new = conn.execute("SELECT * FROM entries WHERE topic='+lang'").fetchone()
    assert pref_new["status"] == "degraded" and pref_new["degraded_reason"] == "conflict_lost"
    new_marks = json.loads(pref_new["conflict_with"])
    assert taboo["id"] in new_marks and pref_old["id"] in new_marks
    assert pref_new["id"] in json.loads(taboo["conflict_with"])
    assert pref_new["id"] in json.loads(pref_old["conflict_with"])
    assert taboo["status"] == "active" and taboo["confidence"] == 0.9  # taboo 恒胜：不降
    assert pref_old["status"] == "degraded" and pref_old["confidence"] < 0.85


def test_cross_scope_coexist(conn, embedder_stub):
    # Review Focus #3：跨作用域同 topic 共存（主键含 scope）
    apply_actions(conn, "w", "u", [add_action("preference", "lang", "global"),
                                   add_action("preference", "lang", "project:x")],
                  embedder_stub, "text", "e1", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 2


def test_missing_quote_rejected(conn, embedder_stub):
    # spec §5 硬约束：无 quote 的条目拒绝入库
    c = full_cand("preference", "lang", "global")
    c["quote"] = None
    apply_actions(conn, "w", "u", [{"op": "ADD", "candidate": c}], embedder_stub, "text", "e1", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
