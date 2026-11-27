"""Task 7 写入器测试：brief 测试 + 裁决钉死（taboo 恒胜/轨迹行/双存变形/向量降级）。"""
import json

import pytest

from pismor.llm.embedder import EmbedUnavailable
from pismor.pipeline.writer import IllegalTransition, apply_actions

NOW = "2026-11-24T00:00:00+08:00"


def full_cand(dimension, topic, body="新值", quote="用户原话"):
    # candidate 键见 extractor：dimension/topic/body/summary/confidence/kind/quote/topic_new
    return {"dimension": dimension, "topic": topic,
            "body": body, "summary": body[:20], "confidence": 0.9,
            "kind": "explicit", "quote": quote, "topic_new": None}


def add_action(dimension="preference", topic="lang"):
    return {"op": "ADD", "candidate": full_cand(dimension, topic, body="首见")}


def update_action(dimension, topic):
    return {"op": "UPDATE", "candidate": full_cand(dimension, topic),
            "target": (dimension, topic)}


def conflict_action(dimension, topic):
    return {"op": "CONFLICT", "candidate": full_cand(dimension, topic, body="相反表述"),
            "target": (dimension, topic)}


def noop_action(dimension, topic):
    return {"op": "NOOP", "candidate": full_cand(dimension, topic),
            "target": (dimension, topic)}


def orphan_action():
    c = full_cand("orphan", "种植多肉")
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
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements,archived_at)"
        " VALUES ('w','u',?,'lang',?,'0.9',?,'旧值','旧值',"
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
    row = conn.execute("SELECT * FROM entries WHERE variant='normal'").fetchone()
    assert row["status"] == "active" and row["topic"] == "lang" and len(row["id"]) == 32
    assert row["last_reinforced_at"] == NOW and row["reinforcements"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1
    src = conn.execute("SELECT * FROM sources").fetchone()
    # sources.entry_id 即 entries.id 行键（{dimension}_{topic}），可直接归属与 JOIN
    assert (src["entry_id"], src["origin_type"], src["origin_ref"], src["kind"]) == \
        (row["id"], "conversation", "s1:1", "explicit")


def test_update_archives_old(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [update_action("preference", "lang")],
                  embedder_stub, "text", "e1", NOW)
    # 轨迹行（topic 加 "~" 前缀+时间戳序号）：旧行完整内容 archived，superseded_by 指向新行 id
    sup = conn.execute("SELECT * FROM entries WHERE variant='trace'").fetchone()
    assert sup["status"] == "archived" and sup["superseded_by"] == "preference_lang"
    assert sup["archived_at"] == NOW and sup["body"] == "旧值"
    # 原行同 key 覆盖为新值 active（bi-temporal 旧值由轨迹行承载）
    cur = conn.execute("SELECT * FROM entries WHERE topic='lang' AND variant='normal'").fetchone()
    assert cur["status"] == "active" and cur["body"] == "新值"
    assert cur["last_reinforced_at"] == NOW and cur["superseded_by"] is None


def test_update_copies_sources_to_superseded_row(conn, embedder_stub, seeded_active):
    # spec §12：archived 条目保留全部字段与证据链——轨迹行复制旧行全部 sources
    conn.executemany(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES ('preference_lang','w','u',?,?,'旧证据','explicit')",
        [("conversation", "s1:1"), ("text", "doc:2")])
    conn.commit()
    apply_actions(conn, "w", "u", [update_action("preference", "lang")],
                  embedder_stub, "text", "e1", NOW)
    trace = conn.execute("SELECT id FROM entries WHERE variant='trace'").fetchone()
    sup_srcs = conn.execute(
        "SELECT * FROM sources WHERE entry_id=? ORDER BY origin_ref", (trace["id"],)).fetchall()
    assert [(s["origin_type"], s["origin_ref"], s["quote"]) for s in sup_srcs] == \
        [("text", "doc:2", "旧证据"), ("conversation", "s1:1", "旧证据")]
    cur_srcs = conn.execute(
        "SELECT * FROM sources WHERE entry_id='preference_lang'").fetchall()
    assert [(s["origin_type"], s["origin_ref"], s["quote"]) for s in cur_srcs] == \
        [("text", "e1", "用户原话")]  # 新 active 行携带新证据


def test_conflict_symmetric(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [conflict_action("preference", "lang")],
                  embedder_stub, "text", "e1", NOW)
    old = conn.execute("SELECT * FROM entries WHERE topic='lang' AND variant='normal'").fetchone()
    new = conn.execute("SELECT * FROM entries WHERE topic='lang' AND variant='conflict'").fetchone()
    # Review Focus #4：对称互标（裸 id 标记，C-1）
    assert new["id"] in json.loads(old["conflict_with"])
    assert old["id"] in json.loads(new["conflict_with"])
    assert old["confidence"] < 0.85 and new["confidence"] < 0.85
    assert old["status"] == "degraded" and old["degraded_reason"] == "conflict_lost"
    assert new["status"] == "degraded" and new["degraded_reason"] == "conflict_lost"


def test_noop_reinforces(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [noop_action("preference", "lang")],
                  embedder_stub, "text", "e1", NOW)
    row = conn.execute("SELECT reinforcements, last_reinforced_at FROM entries").fetchone()
    assert row["reinforcements"] == 3 and row["last_reinforced_at"] == NOW


def _seed_degraded(conn):
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements,degraded_reason)"
        " VALUES ('w','u','preference','lang','preference_lang',0.65,'degraded',"
        "'旧值','旧值','2026-01-01','2026-01-01','2026-01-01',2,'stale')")
    conn.commit()


def test_low_confidence_does_not_revive_degraded(conn, embedder_stub):
    # spec §6：degraded → active 需新强化证据 ≥ 0.85；低置信只计强化，不覆盖不复活
    _seed_degraded(conn)
    action = update_action("preference", "lang")
    action["candidate"]["confidence"] = 0.7
    apply_actions(conn, "w", "u", [action], embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert row["status"] == "degraded" and row["body"] == "旧值"
    assert row["confidence"] == 0.65          # 低置信候选不得覆盖原置信
    assert row["reinforcements"] == 3 and row["last_reinforced_at"] == NOW  # 仍计一次强化
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE topic LIKE '~%'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0
    hist = conn.execute(
        "SELECT * FROM review_history WHERE decision='revive_rejected'").fetchone()
    assert hist["actor"] == "pipeline" and hist["note"].startswith("confidence=0.70")


def test_confidence_at_threshold_revives_degraded(conn, embedder_stub):
    # 边界 0.85 应放行（判定为 < 0.85 拒绝）
    _seed_degraded(conn)
    action = update_action("preference", "lang")
    action["candidate"]["confidence"] = 0.85
    apply_actions(conn, "w", "u", [action], embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT * FROM entries WHERE topic='lang' AND variant='normal'").fetchone()
    assert row["status"] == "active" and row["body"] == "新值"
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE variant='trace'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM review_history").fetchone()["c"] == 0


def test_revive_threshold_follows_gate_auto_min(conn, embedder_stub):
    # 门槛可配置：auto_min 下调到 0.6 时，0.7 的新证据应能复活
    _seed_degraded(conn)
    action = update_action("preference", "lang")
    action["candidate"]["confidence"] = 0.7
    apply_actions(conn, "w", "u", [action], embedder_stub, "conversation", "s1:1", NOW,
                  revive_min_confidence=0.6)
    row = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert row["status"] == "active" and row["body"] == "新值"


def test_revive_threshold_can_be_raised(conn, embedder_stub):
    # auto_min 上调到 0.9 时，0.85 的旧门槛证据不再复活
    _seed_degraded(conn)
    action = update_action("preference", "lang")
    action["candidate"]["confidence"] = 0.85
    apply_actions(conn, "w", "u", [action], embedder_stub, "conversation", "s1:1", NOW,
                  revive_min_confidence=0.9)
    row = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert row["status"] == "degraded" and row["body"] == "旧值"
    assert conn.execute(
        "SELECT COUNT(*) c FROM review_history WHERE decision='revive_rejected'").fetchone()["c"] == 1


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
    row = conn.execute("SELECT id FROM entries WHERE variant='normal'").fetchone()
    assert conn.execute("SELECT entry_id FROM embeddings").fetchone()["entry_id"] == row["id"]


def test_update_refreshes_vector(conn, embedder_stub):
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    apply_actions(conn, "w", "u", [add_action()], embedder_stub, "text", "e1", NOW)
    apply_actions(conn, "w", "u", [update_action("preference", "lang")],
                  embedder_stub, "text", "e2", NOW)
    row = conn.execute("SELECT id FROM entries WHERE variant='normal'").fetchone()
    rows = conn.execute("SELECT entry_id FROM embeddings").fetchall()
    assert [r["entry_id"] for r in rows] == [row["id"]]  # 同一条目覆盖，不重复


def test_orphan_goes_pending(conn, embedder_stub):
    apply_actions(conn, "w", "u", [orphan_action()], embedder_stub, "text", "e1", NOW)
    row = conn.execute("SELECT status, topic, topic_new FROM entries").fetchone()
    assert row["status"] == "orphan_topic" and row["topic_new"]
    assert row["topic"] == row["topic_new"] == "用户谈多肉植物养护"  # 观察区键用 topic_new


def test_orphan_recur_reinforces(conn, embedder_stub):
    # 同主题 orphan 复现：计数 +1（§8.2 启用门槛依据），不重复插行
    apply_actions(conn, "w", "u", [orphan_action()], embedder_stub, "text", "e1", NOW)
    apply_actions(conn, "w", "u", [orphan_action()], embedder_stub, "text", "e2", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1
    assert conn.execute("SELECT reinforcements FROM entries").fetchone()["reinforcements"] == 1


def test_illegal_transition_rejected(conn, seeded_archived, embedder_stub):
    with pytest.raises(IllegalTransition):
        apply_actions(conn, "w", "u", [update_action("preference", "lang")],
                      embedder_stub, "text", "e1", NOW)


def test_illegal_transition_rolls_back_batch(conn, seeded_active, embedder_stub):
    # 整批原子性：合法 NOOP + 非法 UPDATE 同批，抛出后 NOOP 强化也不落库
    actions = [noop_action("preference", "lang"),
               update_action("preference", "lang")]
    actions[1]["target"] = ("preference", "ghost")  # 目标不存在
    with pytest.raises(IllegalTransition):
        apply_actions(conn, "w", "u", actions, embedder_stub, "text", "e1", NOW)
    assert conn.execute("SELECT reinforcements FROM entries").fetchone()["reinforcements"] == 2


def test_batch_taboo_wins_over_preference(conn, embedder_stub):
    # 上游裁决 a：同批双新增乱序给出，写入器内 taboo 先落库，preference 侧恒胜降级
    actions = [add_action("preference", "lang"), add_action("taboo", "lang")]
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
    apply_actions(conn, "w", "u", [conflict_action("preference", "lang")],
                  embedder_stub, "text", "e1", NOW)
    taboo = conn.execute("SELECT * FROM entries WHERE dimension='taboo'").fetchone()
    pref_old = conn.execute("SELECT * FROM entries WHERE dimension='preference' AND topic='lang' AND variant='normal'").fetchone()
    pref_new = conn.execute("SELECT * FROM entries WHERE topic='lang' AND variant='conflict'").fetchone()
    assert pref_new["status"] == "degraded" and pref_new["degraded_reason"] == "conflict_lost"
    new_marks = json.loads(pref_new["conflict_with"])
    assert taboo["id"] in new_marks
    assert pref_old["id"] in new_marks
    assert pref_new["id"] in json.loads(taboo["conflict_with"])
    assert pref_new["id"] in json.loads(pref_old["conflict_with"])
    assert taboo["status"] == "active" and taboo["confidence"] == 0.9  # taboo 恒胜：不降
    assert pref_old["status"] == "degraded" and pref_old["confidence"] < 0.85


def test_missing_quote_rejected(conn, embedder_stub):
    # spec §5 硬约束：无 quote 的条目拒绝入库
    c = full_cand("preference", "lang")
    c["quote"] = None
    apply_actions(conn, "w", "u", [{"op": "ADD", "candidate": c}], embedder_stub, "text", "e1", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


def test_update_twice_creates_distinct_superseded_rows(conn, embedder_stub, seeded_active):
    # Important 回归：同 key 连续两次 UPDATE 不抛异常，两条轨迹行并存，新行为最新值
    first = update_action("preference", "lang")
    apply_actions(conn, "w", "u", [first], embedder_stub, "text", "e1", NOW)
    second = update_action("preference", "lang")
    second["candidate"]["body"] = "最新值"
    apply_actions(conn, "w", "u", [second], embedder_stub, "text", "e2", NOW)
    sups = conn.execute(
        "SELECT topic, body FROM entries WHERE variant='trace' ORDER BY variant_seq").fetchall()
    assert len(sups) == 2
    assert sups[0]["body"] == "旧值" and sups[1]["body"] == "新值"  # 各版本各归其档
    cur = conn.execute("SELECT * FROM entries WHERE topic='lang' AND variant='normal'").fetchone()
    assert cur["status"] == "active" and cur["body"] == "最新值"


# ---- fix wave 新增：复合键 / 澄清语义 / orphan 键 / pending 演进 / 向量四元组 ----

def _seed_row(conn, *, dim="preference", topic="lang", status="active",
              body="旧值", conflict_with=None, updated=NOW, archived_at=None,
              degrad="conflict_lost", variant="normal", variant_seq=0):
    entry_id = f"{dim}_{topic}" if variant == "normal" else f"{dim}_{topic}_{variant}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,variant,variant_seq,"
        "confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,conflict_with,"
        "archived_at) VALUES ('w','u',?,?,?,?,?,0.7,?,?,?,?,?,?,?,?,?)",
        (dim, topic, entry_id, variant, variant_seq, status, body, body[:20],
         "2026-01-01", updated, "2026-01-01",
         degrad if status == "degraded" else None, conflict_with, archived_at))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,'w','u','text','seed','原话','explicit')",
        (entry_id,))
    conn.commit()
    return entry_id


def test_resolve_conflicts_exact_mark_and_status(conn):
    # M-2/M-3：只清精确 id 的存活行标记；archived 轨迹行与近似 id 行不得被改写
    from pismor.pipeline.writer import resolve_conflicts
    _seed_row(conn, topic="styleX",
              conflict_with=json.dumps(["preference_styleX"]))
    _seed_row(conn, topic="styleold", status="archived",
              archived_at=NOW, conflict_with=json.dumps(["preference_style"]))
    # 精确目标行（同 id 不同 topic 的近似行不得被 LIKE 误伤）
    _seed_row(conn, topic="lang",
              conflict_with=json.dumps(["preference_style"]))
    resolve_conflicts(conn, "w", "u", "preference_style", NOW, revive=False)
    rows = {r["topic"]: r["conflict_with"] for r in conn.execute(
        "SELECT topic, conflict_with FROM entries").fetchall()}
    assert rows["lang"] is None                    # 精确命中并被清空
    assert rows["styleX"] is not None              # preference_styleX 不被子串误伤
    assert rows["styleold"] is not None            # archived 轨迹行保持原样（status 过滤）


def test_normal_update_does_not_revive_opponent(conn):
    # M-1：普通演进 UPDATE 只清 conflict_with，不复活冲突对方
    _seed_row(conn, topic="lang",
              conflict_with=json.dumps(["preference_+lang"]))
    _seed_row(conn, topic="lang", status="degraded", variant="conflict", variant_seq=1,
              body="相反偏好", conflict_with=json.dumps(["preference_lang"]))
    # 用真实写路径（apply_actions 需要 embedder，传最小 stub）
    class E:
        def embed(self, texts):
            return [[0.1] * 4 for _ in texts]
    apply_actions(conn, "w", "u", [update_action("preference", "lang")], E(),
                  "text", "e2", NOW)
    main = conn.execute("SELECT status, conflict_with FROM entries WHERE topic='lang' AND variant='normal'").fetchone()
    plus = conn.execute("SELECT status, degraded_reason FROM entries WHERE topic='lang' AND variant='conflict'").fetchone()
    assert main["status"] == "active" and main["conflict_with"] is None
    assert plus["status"] == "degraded" and plus["degraded_reason"] == "conflict_lost"


def test_orphan_key_uses_topic_new_and_empty_not_merged(conn):
    # I-1：空 topic 的不同 topic_new 各自成行，不压成 orphan_；同 topic_new 复用强化
    def orphan(topic_new, body="候选"):
        c = full_cand("orphan", "", body=body)
        c["status"] = "orphan_topic"
        c["topic_new"] = topic_new
        return {"op": "ORPHAN", "candidate": c}
    class E:
        def embed(self, texts):
            return [[0.1] * 4 for _ in texts]
    apply_actions(conn, "w", "u", [orphan("多肉养护"), orphan("茶叶品鉴"), orphan("小说创作")],
                  E(), "text", "e1", NOW)
    rows = conn.execute("SELECT topic, topic_new FROM entries ORDER BY topic").fetchall()
    assert len(rows) == 3
    assert {r["topic"] for r in rows} == {"多肉养护", "茶叶品鉴", "小说创作"}
    assert all(r["topic"] == r["topic_new"] for r in rows)
    # 同 topic_new 复现 → 同键 +1，不插新行
    apply_actions(conn, "w", "u", [orphan("多肉养护", "又聊多肉")], E(), "text", "e2", NOW)
    row = conn.execute("SELECT reinforcements FROM entries WHERE topic='多肉养护'").fetchone()
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 3
    assert row["reinforcements"] == 1


def test_pending_review_row_evolves_without_blocking(conn):
    # I-2：pending_review 同 key 新证据 → 规范 active 行演进；原 pending 快照留复核队列
    _seed_row(conn, topic="style", status="pending_review",
              degrad=None, conflict_with=None)
    class E:
        def embed(self, texts):
            return [[0.1] * 4 for _ in texts]
    apply_actions(conn, "w", "u", [update_action("preference", "style")], E(),
                  "text", "e2", NOW)
    main = conn.execute("SELECT status, body FROM entries WHERE topic='style' AND variant='normal'").fetchone()
    pending = conn.execute(
        "SELECT status, topic, variant FROM entries WHERE status='pending_review'").fetchone()
    assert main["status"] == "active" and main["body"] == "新值"
    assert pending is not None and pending["variant"] == "snapshot"
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 2


def test_vector_rows_keyed_by_four_tuple_and_sqlite_error_degrades(conn):
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    class E:
        def embed(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    apply_actions(conn, "w", "u", [add_action("preference", "lang")],
                  E(), "text", "e1", NOW)
    keys = {r["entry_id"] for r in conn.execute("SELECT entry_id FROM embeddings").fetchall()}
    row = conn.execute("SELECT id FROM entries WHERE topic='lang' AND variant='normal'").fetchone()
    assert keys == {row["id"]}
    # 维度配置错：vec 插入抛 sqlite 错误 → 条目照写、不抛出（spec §20）
    class WrongDim:
        def embed(self, texts):
            return [[0.1] for _ in texts]
    apply_actions(conn, "w", "u", [add_action("preference", "tone2")], WrongDim(),
                  "text", "e2", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE topic='tone2'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM embeddings").fetchone()["c"] == 1  # 失败未写入


def test_entry_id_is_row_key_matching_sources(conn, embedder_stub):
    """entries.id 是稳定行键，sources.entry_id 直接引用它，可直接 JOIN。"""
    apply_actions(conn, "w", "u", [add_action()], embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT id FROM entries").fetchone()
    assert len(row["id"]) == 32
    src = conn.execute("SELECT entry_id FROM sources").fetchone()
    assert src["entry_id"] == row["id"]
    hit = conn.execute(
        "SELECT 1 FROM entries e JOIN sources s"
        " ON s.user_source=e.user_source AND s.user_id=e.user_id AND s.entry_id=e.id"
        " WHERE e.id=?", (row["id"],)).fetchone()
    assert hit is not None


# ---- 审查修复：事务外批量嵌入 / L2 归一化 / 失效标记重定向 / 已应用动作返回值 ----


class CountingEmbedder:
    def __init__(self, vec=None):
        self.calls = []
        self.vec = vec or [0.1, 0.2, 0.3, 0.4]

    def embed(self, texts):
        self.calls.append(list(texts))
        return [list(self.vec) for _ in texts]


def _with_body(action, body):
    action["candidate"]["body"] = body
    return action


def test_vectors_embedded_in_one_batch_outside_write(conn):
    # 审查 #3：嵌入是网络 IO，不得在写事务内逐条发起
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    e = CountingEmbedder()
    apply_actions(conn, "w", "u",
                  [_with_body(add_action("preference", "lang"), "第一条"),
                   _with_body(add_action("preference", "tone"), "第二条")],
                  e, "text", "e1", NOW)
    assert len(e.calls) == 1 and len(e.calls[0]) == 2  # 一次批量请求覆盖全部待嵌文本
    assert conn.execute("SELECT COUNT(*) c FROM embeddings").fetchone()["c"] == 2


def test_vectors_l2_normalized_before_store(conn):
    # 审查 #6：阈值按余弦距离换算，库内向量必须是 L2 归一化后的值
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[2])")
    apply_actions(conn, "w", "u",
                  [_with_body(add_action("preference", "lang"), "归一化")],
                  CountingEmbedder(vec=[3.0, 4.0]), "text", "e1", NOW)
    stored = json.loads(conn.execute(
        "SELECT vec_to_json(embedding) j FROM embeddings").fetchone()["j"])
    assert stored == pytest.approx([0.6, 0.8])


def test_release_pending_redirects_dangling_marks(conn):
    # 审查 #16：pending 行改键后，其他行 conflict_with 里的旧标记必须跟随
    old_id = _seed_row(conn, topic="style", status="pending_review", degrad=None,
                       conflict_with=None)
    _seed_row(conn, topic="tone", status="degraded", conflict_with=json.dumps([old_id]))
    class E:
        def embed(self, texts):
            return [[0.1] * 4 for _ in texts]
    apply_actions(conn, "w", "u", [update_action("preference", "style")], E(),
                  "text", "e2", NOW)
    row = conn.execute("SELECT conflict_with FROM entries WHERE topic='tone'").fetchone()
    assert json.loads(row["conflict_with"]) == ["preference_style"]


def test_apply_actions_returns_only_written_actions(conn):
    # 审查 #17：无 quote 的候选被静默跳过，不得出现在"已应用"列表里
    silent = {"op": "ADD", "candidate": full_cand("preference", "lang", quote=None)}
    written = {"op": "ADD", "candidate": full_cand("preference", "format")}
    applied = apply_actions(conn, "w", "u", [silent, written],
                            EmbedderStub(), "text", "e1", NOW)
    assert [a["candidate"]["topic"] for a in applied] == ["format"]


def test_nan_confidence_cannot_revive_degraded(conn, embedder_stub):
    # F6: NaN 使 `NaN < 0.85` 恒 False，修复前任意 NaN 证据可复活降级条目；
    # 入口钳制后 NaN→0.0，走 revive_rejected
    _seed_degraded(conn)
    action = update_action("preference", "lang")
    action["candidate"]["confidence"] = float("nan")
    apply_actions(conn, "w", "u", [action], embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert row["status"] == "degraded" and row["body"] == "旧值"
    hist = conn.execute(
        "SELECT * FROM review_history WHERE decision='revive_rejected'").fetchone()
    assert hist["note"].startswith("confidence=0.00")


def test_nan_confidence_clamped_on_add(conn, embedder_stub):
    # F6: ADD 路径 NaN/超界值同样钳到 [0,1]
    action = add_action()
    action["candidate"]["confidence"] = float("nan")
    apply_actions(conn, "w", "u", [action], embedder_stub, "text", "e1", NOW)
    assert conn.execute("SELECT confidence FROM entries").fetchone()["confidence"] == 0.0
    action2 = add_action("preference", "tone")
    action2["candidate"]["confidence"] = 42.0
    apply_actions(conn, "w", "u", [action2], embedder_stub, "text", "e2", NOW)
    row = conn.execute(
        "SELECT confidence FROM entries WHERE topic='tone'").fetchone()
    assert row["confidence"] == 1.0


def test_revive_restores_confidence_to_max(conn, embedder_stub):
    # F7: 复活条目的置信取 max(降级残值, 新证据)；0.8ⁿ 循环衰减不再让复活条目沉底
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements,degraded_reason)"
        " VALUES ('w','u','preference','lang','preference_lang',0.41,'degraded',"
        "'旧值','旧值','2026-01-01','2026-01-01','2026-01-01',2,'conflict_lost')")
    conn.commit()
    action = update_action("preference", "lang")
    action["candidate"]["confidence"] = 0.86
    apply_actions(conn, "w", "u", [action], embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert row["status"] == "active"
    assert row["confidence"] == 0.86  # max(0.41, 0.86)


def test_superseded_seq_not_reused_after_merge_deletion(conn, embedder_stub, seeded_active):
    # F8: 轨迹行被归并物理删除后，COUNT+1 会按剩余数复用现存序号撞主键；
    # 改取现存键最大序号 +1，删除后新轨迹序号只增不减
    apply_actions(conn, "w", "u", [update_action("preference", "lang")],
                  embedder_stub, "conversation", "s1:1", NOW)  # 轨迹 :1
    apply_actions(conn, "w", "u", [update_action("preference", "lang")],
                  embedder_stub, "conversation", "s1:2", NOW)  # 轨迹 :2
    trace1 = conn.execute(
        "SELECT id FROM entries WHERE variant='trace' AND variant_seq=1").fetchone()
    conn.execute("DELETE FROM entries WHERE id=?", (trace1["id"],))
    conn.commit()
    # 剩余轨迹仅 :2；修复前 COUNT=1 → seq=2 撞主键 IntegrityError，修复后 seq=3
    apply_actions(conn, "w", "u", [update_action("preference", "lang")],
                  embedder_stub, "conversation", "s1:3", NOW)
    seqs = {r["variant_seq"] for r in conn.execute(
        "SELECT variant_seq FROM entries WHERE variant='trace'").fetchall()}
    assert seqs == {2, 3}


def test_conflict_degradation_refreshes_reinforced_at(conn, embedder_stub, seeded_active):
    # F11: 矛盾降级旧行时刷新 last_reinforced_at，刚落败的重要记忆不再提前进入衰减窗口
    apply_actions(conn, "w", "u", [conflict_action("preference", "lang")],
                  embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert row["status"] == "degraded"
    assert row["last_reinforced_at"] == NOW  # seed 时为 2026-01-01
