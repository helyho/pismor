"""Task 7 写入器测试：brief 测试 + 裁决钉死（taboo 恒胜/轨迹行/双存变形/向量降级）。"""
import json

import pytest

from pismor.llm.embedder import EmbedUnavailable
from pismor.core.keys import mark, vec_key
from pismor.pipeline.writer import IllegalTransition, apply_actions

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
    assert row["status"] == "active" and row["id"] == "preference_lang@global"
    assert row["last_reinforced_at"] == NOW and row["reinforcements"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1
    src = conn.execute("SELECT * FROM sources").fetchone()
    # sources.entry_id 为行唯一键 {entries.id}@{scope}（id 不含 scope，跨 scope 证据可归属）
    assert (src["entry_id"], src["origin_type"], src["origin_ref"], src["kind"]) == \
        ("preference_lang@global", "conversation", "s1:1", "explicit")


def test_update_archives_old(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    # 轨迹行（topic 加 "~" 前缀+时间戳序号）：旧行完整内容 archived，superseded_by 指向新行 id
    sup = conn.execute("SELECT * FROM entries WHERE topic LIKE '~lang:%'").fetchone()
    assert sup["status"] == "archived" and sup["superseded_by"] == "preference_lang@global"
    assert sup["archived_at"] == NOW and sup["body"] == "旧值"
    # 原行同 key 覆盖为新值 active（bi-temporal 旧值由轨迹行承载）
    cur = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert cur["status"] == "active" and cur["body"] == "新值"
    assert cur["last_reinforced_at"] == NOW and cur["superseded_by"] is None


def test_update_copies_sources_to_superseded_row(conn, embedder_stub, seeded_active):
    # spec §12：archived 条目保留全部字段与证据链——轨迹行复制旧行全部 sources
    conn.executemany(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES ('preference_lang@global','w','u',?,?,'旧证据','explicit')",
        [("conversation", "s1:1"), ("text", "doc:2")])
    conn.commit()
    apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    sup_srcs = conn.execute(
        "SELECT * FROM sources WHERE entry_id LIKE 'preference_~lang:%' ORDER BY origin_ref").fetchall()
    assert [(s["origin_type"], s["origin_ref"], s["quote"]) for s in sup_srcs] == \
        [("text", "doc:2", "旧证据"), ("conversation", "s1:1", "旧证据")]
    cur_srcs = conn.execute(
        "SELECT * FROM sources WHERE entry_id='preference_lang@global'").fetchall()
    assert [(s["origin_type"], s["origin_ref"], s["quote"]) for s in cur_srcs] == \
        [("text", "e1", "用户原话")]  # 新 active 行携带新证据


def test_conflict_symmetric(conn, embedder_stub, seeded_active):
    apply_actions(conn, "w", "u", [conflict_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    old = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    new = conn.execute("SELECT * FROM entries WHERE topic='+lang'").fetchone()
    # Review Focus #4：对称互标（标记带 scope，C-1）
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
    assert conn.execute("SELECT entry_id FROM embeddings").fetchone()["entry_id"] == \
        vec_key("w", "u", "preference", "lang", "global")


def test_update_refreshes_vector(conn, embedder_stub):
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    apply_actions(conn, "w", "u", [add_action()], embedder_stub, "text", "e1", NOW)
    apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")],
                  embedder_stub, "text", "e2", NOW)
    rows = conn.execute("SELECT entry_id FROM embeddings").fetchall()
    assert [r["entry_id"] for r in rows] == \
        [vec_key("w", "u", "preference", "lang", "global")]  # 同五元组覆盖，不重复


def test_orphan_goes_pending(conn, embedder_stub):
    apply_actions(conn, "w", "u", [orphan_action()], embedder_stub, "text", "e1", NOW)
    row = conn.execute("SELECT status, topic, topic_new FROM entries").fetchone()
    assert row["status"] == "orphan_topic" and row["topic_new"]
    assert row["topic"] == row["topic_new"] == "用户谈多肉植物养护"  # 观察区键用 topic_new


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
    assert taboo["id"] in new_marks
    assert pref_old["id"] in new_marks
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


def _seed_with_source(conn, scope, origin_ref, quote):
    """seed 一行带 1 条独立证据的 active 条目（跨 scope 同 topic，id 相同）。"""
    entry_id = f"preference_lang@{scope}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at)"
        " VALUES ('w','u','preference','lang',?,?,'0.9','active',?,?,'2026-01-01','2026-01-01','2026-01-01')",
        (scope, entry_id, f"旧值@{scope}", f"旧值@{scope}"))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,'w','u','text',?,?,'explicit')", (entry_id, origin_ref, quote))
    conn.commit()


def test_update_moves_sources_by_full_key(conn, embedder_stub):
    # Critical 回归：跨 scope 同 topic 共存（id 相同）时，UPDATE global 行
    # 不得转移 project:x 行的证据——转移按行唯一键（id@scope）精确圈定
    _seed_with_source(conn, "global", "doc:g1", "global旧证据")
    _seed_with_source(conn, "project:x", "doc:x1", "project旧证据")
    apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")],
                  embedder_stub, "text", "e1", NOW)
    # project:x 行证据原样未动（键未被转移）
    assert [(s["origin_ref"], s["quote"]) for s in conn.execute(
        "SELECT origin_ref, quote FROM sources WHERE entry_id='preference_lang@project:x'").fetchall()] \
        == [("doc:x1", "project旧证据")]
    # global 轨迹行持有且仅持有 global 旧行的证据
    sup = conn.execute("SELECT id FROM entries WHERE topic LIKE '~lang:%'").fetchone()
    sup_srcs = conn.execute(
        "SELECT origin_ref, quote FROM sources WHERE entry_id=? ORDER BY origin_ref",
        (sup["id"],)).fetchall()
    assert [(s["origin_ref"], s["quote"]) for s in sup_srcs] == [("doc:g1", "global旧证据")]
    # global 原 key 行（新值 active）仅携带新证据
    cur_srcs = conn.execute(
        "SELECT origin_ref FROM sources WHERE entry_id='preference_lang@global'"
        " AND origin_ref != 'doc:g1'").fetchall()
    assert [(s["origin_ref"],) for s in cur_srcs] == [("e1",)]


def test_update_twice_creates_distinct_superseded_rows(conn, embedder_stub, seeded_active):
    # Important 回归：同 key 连续两次 UPDATE 不抛异常，两条轨迹行并存，新行为最新值
    first = update_action("preference", "lang", "global")
    apply_actions(conn, "w", "u", [first], embedder_stub, "text", "e1", NOW)
    second = update_action("preference", "lang", "global")
    second["candidate"]["body"] = "最新值"
    apply_actions(conn, "w", "u", [second], embedder_stub, "text", "e2", NOW)
    sups = conn.execute(
        "SELECT topic, body FROM entries WHERE topic LIKE '~lang:%' ORDER BY topic").fetchall()
    assert len(sups) == 2
    assert sups[0]["body"] == "旧值" and sups[1]["body"] == "新值"  # 各版本各归其档
    cur = conn.execute("SELECT * FROM entries WHERE topic='lang'").fetchone()
    assert cur["status"] == "active" and cur["body"] == "最新值"


# ---- fix wave 新增：复合键 / 澄清语义 / orphan 键 / pending 演进 / 向量五元组 ----

def _seed_row(conn, *, dim="preference", topic="lang", scope="global", status="active",
              body="旧值", conflict_with=None, updated=NOW, archived_at=None,
              degrad="conflict_lost"):
    entry_id = f"{dim}_{topic}@{scope}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,degraded_reason,conflict_with,"
        "archived_at) VALUES ('w','u',?,?,?,?,0.7,?,?,?,?,?,?,?,?,?)",
        (dim, topic, scope, entry_id, status, body, body[:20], "2026-01-01", updated,
         "2026-01-01", degrad if status == "degraded" else None, conflict_with, archived_at))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,'w','u','text','seed','原话','explicit')",
        (entry_id,))
    conn.commit()
    return entry_id


def test_resolve_conflicts_exact_mark_scope_and_status(conn):
    # M-2/M-3：只清精确 (id,scope) 的存活行标记；archived 轨迹行与近似 id 行不得被改写
    from pismor.core.keys import mark
    from pismor.pipeline.writer import resolve_conflicts
    _seed_row(conn, topic="styleX", scope="global",
              conflict_with=json.dumps([mark("preference_styleX", "global")]))
    _seed_row(conn, topic="styleold", scope="global", status="archived",
              archived_at=NOW, conflict_with=json.dumps([mark("preference_style", "global")]))
    # 精确目标行（同 id 不同 topic 的近似行不得被 LIKE 误伤）
    _seed_row(conn, topic="lang", scope="global",
              conflict_with=json.dumps([mark("preference_style", "global")]))
    resolve_conflicts(conn, "w", "u", "preference_style", "global", NOW, revive=False)
    rows = {r["topic"]: r["conflict_with"] for r in conn.execute(
        "SELECT topic, conflict_with FROM entries").fetchall()}
    assert rows["lang"] is None                    # 精确命中并被清空
    assert rows["styleX"] is not None              # preference_styleX 不被子串误伤
    assert rows["styleold"] is not None            # archived 轨迹行保持原样（status 过滤）


def test_cross_scope_conflict_marks_do_not_cross(conn):
    # 同 id 跨 scope：global 的冲突收尾不得清 project:x 的标记
    from pismor.core.keys import mark
    from pismor.pipeline.writer import resolve_conflicts
    _seed_row(conn, topic="lang", scope="global",
              conflict_with=json.dumps([mark("preference_style", "project:x")]))
    _seed_row(conn, topic="lang", scope="project:x",
              conflict_with=json.dumps([mark("preference_style", "project:x")]))
    resolve_conflicts(conn, "w", "u", "preference_style", "global", NOW, revive=False)
    rows = {(r["scope"]): r["conflict_with"] for r in conn.execute(
        "SELECT scope, conflict_with FROM entries WHERE topic='lang'").fetchall()}
    assert rows["global"] is not None and rows["project:x"] is not None


def test_normal_update_does_not_revive_opponent(conn):
    # M-1：普通演进 UPDATE 只清 conflict_with，不复活冲突对方
    from pismor.core.keys import mark
    _seed_row(conn, topic="lang", scope="global",
              conflict_with=json.dumps([mark("preference_+lang", "global")]))
    _seed_row(conn, topic="+lang", scope="global", status="degraded",
              body="相反偏好", conflict_with=json.dumps([mark("preference_lang", "global")]))
    # 用真实写路径（apply_actions 需要 embedder，传最小 stub）
    class E:
        def embed(self, texts):
            return [[0.1] * 4 for _ in texts]
    apply_actions(conn, "w", "u", [update_action("preference", "lang", "global")], E(),
                  "text", "e2", NOW)
    main = conn.execute("SELECT status, conflict_with FROM entries WHERE topic='lang'").fetchone()
    plus = conn.execute("SELECT status, degraded_reason FROM entries WHERE topic='+lang'").fetchone()
    assert main["status"] == "active" and main["conflict_with"] is None
    assert plus["status"] == "degraded" and plus["degraded_reason"] == "conflict_lost"


def test_orphan_key_uses_topic_new_and_empty_not_merged(conn):
    # I-1：空 topic 的不同 topic_new 各自成行，不压成 orphan_；同 topic_new 复用强化
    def orphan(topic_new, body="候选"):
        c = full_cand("orphan", "", "global", body=body)
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
    _seed_row(conn, topic="style", scope="global", status="pending_review",
              degrad=None, conflict_with=None)
    class E:
        def embed(self, texts):
            return [[0.1] * 4 for _ in texts]
    apply_actions(conn, "w", "u", [update_action("preference", "style", "global")], E(),
                  "text", "e2", NOW)
    main = conn.execute("SELECT status, body FROM entries WHERE topic='style'").fetchone()
    pending = conn.execute(
        "SELECT status, topic FROM entries WHERE status='pending_review'").fetchone()
    assert main["status"] == "active" and main["body"] == "新值"
    assert pending is not None and pending["topic"].startswith("!")
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 2


def test_vector_rows_keyed_by_five_tuple_and_sqlite_error_degrades(conn):
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    class E:
        def embed(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    # 同 id 跨 scope 两条 → 两个向量行，互不覆盖
    apply_actions(conn, "w", "u", [add_action("preference", "lang", "global"),
                                   add_action("preference", "lang", "project:x")],
                  E(), "text", "e1", NOW)
    keys = {r["entry_id"] for r in conn.execute("SELECT entry_id FROM embeddings").fetchall()}
    assert keys == {vec_key("w", "u", "preference", "lang", "global"),
                    vec_key("w", "u", "preference", "lang", "project:x")}
    # 维度配置错：vec 插入抛 sqlite 错误 → 条目照写、不抛出（spec §20）
    class WrongDim:
        def embed(self, texts):
            return [[0.1] for _ in texts]
    apply_actions(conn, "w", "u", [add_action("preference", "tone2", "global")], WrongDim(),
                  "text", "e2", NOW)
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE topic='tone2'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM embeddings").fetchone()["c"] == 2  # 失败未写入


def test_entry_id_is_row_key_matching_sources(conn, embedder_stub):
    """entries.id 与 sources.entry_id 同为行键 {dim}_{topic}@{scope}，可直接 JOIN。"""
    apply_actions(conn, "w", "u", [add_action()], embedder_stub, "conversation", "s1:1", NOW)
    row = conn.execute("SELECT id, scope FROM entries").fetchone()
    assert row["id"] == "preference_lang@global"
    src = conn.execute("SELECT entry_id FROM sources").fetchone()
    assert src["entry_id"] == row["id"]
    hit = conn.execute(
        "SELECT 1 FROM entries e JOIN sources s"
        " ON s.user_source=e.user_source AND s.user_id=e.user_id AND s.entry_id=e.id"
        " WHERE e.id=?", (row["id"],)).fetchone()
    assert hit is not None
