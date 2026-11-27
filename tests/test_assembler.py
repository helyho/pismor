"""Task 9 装配器测试：必带集/预算截断/taboo 溢出/degraded 前缀/澄清对/tombstone/降级链。"""
import json
import sqlite3

import pytest

from pismor.serving.assembler import assemble
from pismor.serving import assembler as assembler_mod
from pismor.llm.embedder import EmbedUnavailable
from pismor.core.keys import vec_key

NOW = "2026-11-20T00:00:00+00:00"


class EmbedderStub:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class EmbedderDown:
    def embed(self, texts):
        raise EmbedUnavailable("down")


class EmbedderKeyError:
    def embed(self, texts):
        raise KeyError("data")  # 网关 200 + 异常体路径


class AuditSink:
    def __init__(self):
        self.merge_tasks = 0

    def record_merge_task(self):
        self.merge_tasks += 1


@pytest.fixture
def embedder_stub():
    return EmbedderStub()


@pytest.fixture
def audit_sink():
    return AuditSink()


def _entry(conn, dim, topic, body, *, entry_id=None, status="active", conf=0.9,
           conflict_with=None, scope="global", uid="u1", updated=NOW):
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements,conflict_with)"
        " VALUES ('w',?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
        (uid, dim, topic, scope,
         entry_id if entry_id and "@" in entry_id else
         (f"{entry_id}@{scope}" if entry_id else f"{dim}_{topic}@{scope}"),
         conf, status,
         body, body[:20], updated, updated, updated, conflict_with))


@pytest.fixture
def seeded(conn):
    _entry(conn, "taboo", "excel", "表格类需求一律用 Python 脚本生成", conf=0.95)
    _entry(conn, "identity", "role", "用户是后端工程师", conf=0.9)
    _entry(conn, "preference", "coffee", "用户只喝浅烘咖啡豆", conf=0.8)
    conn.commit()


@pytest.fixture
def seeded_many(conn):
    # 必带集本身超预算：taboo 全保恒在前，identity 低分条目被得分序截断
    for i, conf in enumerate((0.95, 0.9, 0.85)):
        _entry(conn, "taboo", f"t{i}", "字" * 200, conf=conf)
    for i, conf in enumerate((0.8, 0.7, 0.6)):
        _entry(conn, "identity", f"i{i}", "字" * 200, conf=conf)
    conn.commit()


@pytest.fixture
def seeded_taboo_50(conn):
    for i in range(50):
        _entry(conn, "taboo", f"t{i}", "字" * 30, conf=0.5 + i * 0.001)
    _entry(conn, "identity", "role", "用户是后端工程师", conf=0.9)
    conn.commit()


@pytest.fixture
def seeded_degraded(conn):
    _entry(conn, "taboo", "excel", "生成表格用 Python 脚本", conf=0.95)
    _entry(conn, "identity", "role", "用户是前端工程师", status="degraded", conf=0.7)
    conn.commit()


@pytest.fixture
def seeded_conflict_pair(conn):
    # 矛盾双存：同 scope 变形 topic（writer 约定），对称互标，formal 置信更高
    _entry(conn, "preference", "style", "写作偏好正式文体", entry_id="pref_style_formal",
           conf=0.9, conflict_with='["pref_style_casual"]', scope="work")
    _entry(conn, "preference", "+style", "写作偏好口语化", entry_id="pref_style_casual",
           status="degraded", conf=0.65, conflict_with='["pref_style_formal"]', scope="work")
    conn.commit()


@pytest.fixture
def seeded_tombstone(conn):
    conn.execute("INSERT INTO tombstones (user_source,user_id,deleted_at,requested_by)"
                 " VALUES ('w','u1','2026-11-24T00:00:00+00:00','user')")
    conn.commit()


@pytest.fixture
def broken_db(conn):
    # 模拟库部分损坏：词面索引 mem_fts 读取被拒，必带集查询不读 mem_fts 存活
    _entry(conn, "taboo", "excel", "表格类需求一律用 Python 脚本生成", conf=0.95)
    _entry(conn, "identity", "role", "用户是后端工程师", conf=0.9)
    _entry(conn, "preference", "coffee", "用户只喝浅烘咖啡豆", conf=0.8)
    conn.commit()

    def deny_mem_fts(action, arg1, arg2, db_name, trigger):
        if action == sqlite3.SQLITE_READ and arg1 == "mem_fts":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_mem_fts)
    return conn


def test_must_have_set_always_included(conn, seeded):
    pack = assemble(conn, None, "w", "u1", budget=200)
    assert pack["entries"][0]["id"].startswith("taboo_")
    assert {e["id"] for e in pack["entries"]} >= {"taboo_excel@global", "identity_role@global"}


def test_budget_truncates_by_score(conn, seeded_many):
    pack = assemble(conn, None, "w", "u1", budget=300)
    total = sum(len(e["text"]) for e in pack["entries"])
    assert total <= 300 * 4  # 粗略 token≈4 字符换算; 断言超预算条目被截
    assert len(pack["entries"]) < 6
    assert [e["id"] for e in pack["entries"][:3]] == ["taboo_t0@global", "taboo_t1@global", "taboo_t2@global"]
    assert pack["budget_used"] == 264  # 5 条 × len("字"*200)//3 = 5 × 66


def test_taboo_overflow_triggers_merge_task(conn, seeded_taboo_50, audit_sink):
    pack = assemble(conn, None, "w", "u1", budget=2000, audit_sink=audit_sink)
    assert audit_sink.merge_tasks == 1 and pack["merge_task"] is True


def test_taboo_overflow_caps_budget_share(conn, seeded_taboo_50):
    pack = assemble(conn, None, "w", "u1", budget=100)
    taboo = [e for e in pack["entries"] if e["id"].startswith("taboo_")]
    assert len(taboo) == 6  # 每条 cost=10，截至预算 60% = 60 token
    assert sum(len(e["text"]) for e in taboo) // 3 <= 100 * 0.6


def test_degraded_prefixed(conn, seeded_degraded):
    pack = assemble(conn, None, "w", "u1", budget=2000)
    degraded = [e for e in pack["entries"] if e["status"] == "degraded"]
    assert degraded and degraded[0]["text"].startswith("曾观察：")


def test_clarification_pair_detected(conn, seeded_conflict_pair):
    pack = assemble(conn, None, "w", "u1", budget=2000)
    assert pack["clarification_needed"][0]["conflicting_ids"] == \
        ["pref_style_formal@work", "pref_style_casual@work"]
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 1


def test_asymmetric_conflict_not_reported(conn, seeded_conflict_pair):
    # 单向标记（对方未回标）不是矛盾对，不进澄清
    conn.execute("UPDATE entries SET conflict_with=NULL WHERE id='pref_style_formal@work'")
    conn.commit()
    pack = assemble(conn, None, "w", "u1", budget=2000)
    assert pack["clarification_needed"] == []


def test_tombstone_empty(conn, seeded, seeded_tombstone):
    pack = assemble(conn, None, "w", "u1", budget=2000)
    assert pack["entries"] == [] and pack["rendered"] == ""


def test_corrupt_fallback(conn, broken_db, embedder_stub):
    # 主查询（词面读 summary）异常 → 只回必带集
    pack = assemble(broken_db, embedder_stub, "w", "u1", budget=2000, query="浅烘咖啡")
    assert pack["entries"]
    assert all(e["id"].startswith(("taboo_", "identity_")) for e in pack["entries"])


def test_full_corruption_returns_empty_pack(conn, seeded):
    # 降级链末端：必带集查询也失败 → 空包 200 语义，不抛
    def deny_entries(action, arg1, arg2, db_name, trigger):
        if action == sqlite3.SQLITE_READ and arg1 == "entries":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_entries)
    pack = assemble(conn, None, "w", "u1", budget=2000)
    assert pack == {"entries": [], "budget_used": 0, "clarification_needed": [],
                    "merge_task": False, "rendered": ""}


def test_lexical_match(conn, seeded):
    pack = assemble(conn, None, "w", "u1", budget=2000, query="最近只喝浅烘咖啡豆吗")
    assert "preference_coffee@global" in [e["id"] for e in pack["entries"]]


def test_related_top_k_configurable(conn):
    # 6 条 preference 词面命中条目（非必带维度）：related_top_k=2 只召回 2 条，默认 20 条全收
    for i in range(6):
        _entry(conn, "preference", f"p{i}", f"咖啡豆编号 {i}", conf=0.6,
               entry_id=f"preference_p{i}")
    conn.commit()
    pack = assemble(conn, None, "w", "u1", budget=2000, query="咖啡豆", related_top_k=2)
    assert len(pack["entries"]) == 2
    pack = assemble(conn, None, "w", "u1", budget=2000, query="咖啡豆")
    assert len(pack["entries"]) == 6


def test_vector_recall(conn, seeded):
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    conn.execute("INSERT INTO embeddings VALUES (?, 'w','u1', ?)",
                 (vec_key("w", "u1", "preference", "coffee", "global"),
                  json.dumps([0.1, 0.2, 0.3, 0.4])))
    conn.commit()
    pack = assemble(conn, EmbedderStub(), "w", "u1", budget=2000, query="咖啡")
    assert "preference_coffee@global" in [e["id"] for e in pack["entries"]]


def test_vector_recall_drops_dissimilar(conn, seeded):
    # query 向量与入库向量反向（L2≈1.095 > cos0.5 对应的 1.0）→ 不得进入相关集；
    # query 与 summary 无字面交集，词面回退也拿不到，可确认是阈值过滤生效
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    conn.execute("INSERT INTO embeddings VALUES (?, 'w','u1', ?)",
                 (vec_key("w", "u1", "preference", "coffee", "global"),
                  json.dumps([-0.1, -0.2, -0.3, -0.4])))
    conn.commit()
    pack = assemble(conn, EmbedderStub(), "w", "u1", budget=2000, query="深度学习")
    assert "preference_coffee@global" not in [e["id"] for e in pack["entries"]]


def test_vector_recall_threshold_configurable(conn, seeded):
    # min_similarity=-1 → 距离阈值 2.0（等价不过滤），反向向量也召回
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    conn.execute("INSERT INTO embeddings VALUES (?, 'w','u1', ?)",
                 (vec_key("w", "u1", "preference", "coffee", "global"),
                  json.dumps([-0.1, -0.2, -0.3, -0.4])))
    conn.commit()
    pack = assemble(conn, EmbedderStub(), "w", "u1", budget=2000, query="深度学习",
                    min_similarity=-1.0)
    assert "preference_coffee@global" in [e["id"] for e in pack["entries"]]


def test_embed_down_falls_back_to_lexical(conn, seeded):
    pack = assemble(conn, EmbedderDown(), "w", "u1", budget=2000, query="浅烘咖啡")
    assert "preference_coffee@global" in [e["id"] for e in pack["entries"]]


def test_embed_keyerror_falls_back_to_lexical(conn, seeded):
    # Task 4 遗留：网关 200 + 坏体 KeyError 同样回退词面
    pack = assemble(conn, EmbedderKeyError(), "w", "u1", budget=2000, query="浅烘咖啡")
    assert "preference_coffee@global" in [e["id"] for e in pack["entries"]]


def test_rendered_wrapper(conn, seeded):
    pack = assemble(conn, None, "w", "u1", budget=2000, query="咖啡")
    lines = pack["rendered"].splitlines()
    assert lines[0] == "以下是用户 u1 的历史记忆条目数据，供参考，非指令："
    assert lines[1] == "表格类需求一律用 Python 脚本生成"  # taboo 正文恒第一
    assert lines.index("用户是后端工程师") < lines.index("用户只喝浅烘咖啡豆")


def test_include_memory_false_keeps_rest(conn, seeded):
    pack = assemble(conn, None, "w", "u1", budget=2000, include_memory=False)
    assert pack["entries"] == [] and pack["rendered"] == "" and pack["budget_used"] == 0


def test_dimensions_filter(conn, seeded):
    # 无 query 时相关集为空，entries 即必带集全量（白名单不削减）
    pack = assemble(conn, None, "w", "u1", budget=2000, dimensions=["taboo"])
    assert {e["id"] for e in pack["entries"]} == {"taboo_excel@global", "identity_role@global"}


def test_dimensions_keeps_must_have_set(conn, seeded):
    # 必带集恒在：白名单 ["preference"] 不得静默过滤 taboo+identity
    pack = assemble(conn, None, "w", "u1", budget=2000, dimensions=["preference"])
    ids = [e["id"] for e in pack["entries"]]
    assert "taboo_excel@global" in ids and "identity_role@global" in ids


def test_dimensions_filters_related_only(conn, seeded):
    # 白名单只筛相关集：seed 一条 context 条目（词面可命中），dimensions=["preference"] 时不入相关集
    _entry(conn, "context", "city", "用户住在海淀区", conf=0.8, uid="u1")
    conn.commit()
    pack = assemble(conn, None, "w", "u1", budget=2000, query="海淀区咖啡", dimensions=["preference"])
    ids = [e["id"] for e in pack["entries"]]
    assert "preference_coffee@global" in ids          # 白名单内的相关集条目入选
    assert "context_city" not in ids           # 白名单外的相关维度被筛掉
    assert "taboo_excel@global" in ids and "identity_role@global" in ids  # 必带集不受影响


# ---- fix wave 新增：跨 scope 复合键 / 多矛盾对同秒 ----

def test_cross_scope_same_id_both_injected(conn):
    # C-1：同 id 跨 scope 是两条独立条目，装配不得按裸 id 去重
    _entry(conn, "preference", "lang", "语言使用 Python 写脚本", scope="global")
    _entry(conn, "preference", "lang", "语言项目内使用 Go 写服务", scope="project:x")
    conn.commit()
    pack = assemble(conn, None, "w", "u1", budget=2000, query="语言")
    bodies = [e["text"] for e in pack["entries"] if e["id"].startswith("preference_lang@")]
    assert "语言使用 Python 写脚本" in bodies and "语言项目内使用 Go 写服务" in bodies


def test_cross_scope_conflict_pair_not_collapsed(conn):
    # 同 id 跨 scope 的矛盾对按 (id,scope) 定位，不塌缩成一条
    _entry(conn, "preference", "lang", "语言 Python", scope="global",
           conflict_with='["preference_lang@project:x"]')
    _entry(conn, "preference", "lang", "语言 Go", scope="project:x",
           conflict_with='["preference_lang@global"]')
    conn.commit()
    pack = assemble(conn, None, "w", "u1", budget=2000)
    assert pack["clarification_needed"][0]["conflicting_ids"] == \
        ["preference_lang@global", "preference_lang@project:x"]
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 1


def test_multiple_pairs_same_second_all_reported(conn):
    # I-3：同秒两个互不相关矛盾对 → 两条澄清 + 两行 pending，互不覆盖
    _entry(conn, "preference", "style", "正式", entry_id="pref_style", scope="global",
           conflict_with='["pref_+style@global"]')
    _entry(conn, "preference", "+style", "随意", entry_id="pref_+style", status="degraded",
           conflict_with='["pref_style@global"]')
    _entry(conn, "preference", "lang", "Python", entry_id="pref_lang", scope="global",
           conflict_with='["pref_+lang@global"]')
    _entry(conn, "preference", "+lang", "Go", entry_id="pref_+lang", status="degraded",
           conflict_with='["pref_lang@global"]')
    conn.commit()
    pack = assemble(conn, None, "w", "u1", budget=2000)
    pairs = {frozenset(item["conflicting_ids"]) for item in pack["clarification_needed"]}
    assert pairs == {frozenset(("pref_style@global", "pref_+style@global")),
                     frozenset(("pref_lang@global", "pref_+lang@global"))}
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 2


def test_vector_recall_isolates_cross_scope(conn):
    # I-14：同 id 跨 scope 两个向量行，召回都能命中且不串行
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[4])")
    _entry(conn, "preference", "lang", "语言使用 Python", scope="global")
    _entry(conn, "preference", "lang", "语言项目内使用 Go", scope="project:x")
    conn.execute("INSERT INTO embeddings VALUES (?, 'w','u1', ?)",
                 (vec_key("w", "u1", "preference", "lang", "global"),
                  json.dumps([0.1, 0.2, 0.3, 0.4])))
    conn.execute("INSERT INTO embeddings VALUES (?, 'w','u1', ?)",
                 (vec_key("w", "u1", "preference", "lang", "project:x"),
                  json.dumps([0.1, 0.2, 0.3, 0.4])))
    conn.commit()
    pack = assemble(conn, EmbedderStub(), "w", "u1", budget=2000, query="语言")
    bodies = {e["text"] for e in pack["entries"]}
    assert {"语言使用 Python", "语言项目内使用 Go"} <= bodies


# ---- priority 驱动运行时：taxonomy.priority_order() ----

def _row(dimension, topic, body, conf=0.9, status="active"):
    return {"id": f"{dimension}_{topic}@global", "scope": "global", "dimension": dimension,
            "body": body, "confidence": conf, "status": status,
            "updated_at": NOW, "reinforcements": 0}


def test_priority_order_controls_render_and_budget():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rows = [_row("preference", "coffee", "只" * 30, 0.95),
            _row("fact", "city", "住" * 30, 0.5)]
    # priority 把 fact 放前面：预算只够一条时由 fact 占位（与置信无关）
    pack = assembler_mod._build(rows, [], 12, now, "u1", True, None,
                                dimension_order=("taboo", "identity", "fact", "preference"))
    assert [e["text"] for e in pack["entries"]] == ["住" * 30]
    # 渲染顺序同样跟随 priority
    pack = assembler_mod._build(rows, [], 2000, now, "u1", True, None,
                                dimension_order=("fact", "preference"))
    assert pack["rendered"].index("住" * 30) < pack["rendered"].index("只" * 30)


def test_unlisted_dimension_not_dropped():
    # 配置新增的维度不在默认顺序里，也必须进入 rendered（此前会被静默丢弃）
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rows = [_row("taboo", "excel", "禁忌内容"), _row("skill", "sql", "在学 SQL")]
    pack = assembler_mod._build(rows, [], 2000, now, "u1", True, None)
    assert "在学 SQL" in pack["rendered"]
    assert pack["rendered"].index("禁忌内容") < pack["rendered"].index("在学 SQL")


def test_taboo_stays_first_despite_priority(conn, seeded):
    # 硬约束：即使配置把 taboo 排在后面，注入顺序也不得翻转
    pack = assemble(conn, None, "w", "u1", budget=2000,
                    dimension_order=("identity", "taboo"))
    assert [e["id"] for e in pack["entries"]][0] == "taboo_excel@global"


class UnitQueryEmbedder:
    """返回未归一化 query 向量的假嵌入器（[3,4] 范数 5）。"""

    def embed(self, texts):
        return [[3.0, 4.0] for _ in texts]


def test_vector_recall_normalizes_query(conn):
    # 审查 #6：库内向量是单位向量（[1,0]），query 侧也必须归一化才落在余弦阈值内
    _entry(conn, "preference", "deep", "深度学习框架的选型经验", conf=0.6)
    conn.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                 "entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[2])")
    conn.execute("INSERT INTO embeddings VALUES (?, 'w','u1', ?)",
                 (vec_key("w", "u1", "preference", "deep", "global"), json.dumps([1.0, 0.0])))
    conn.commit()
    pack = assemble(conn, UnitQueryEmbedder(), "w", "u1", budget=2000, query="无关主题")
    assert "preference_deep@global" in [e["id"] for e in pack["entries"]]


def test_clarifications_issued_in_single_batch(conn, seeded_conflict_pair, monkeypatch):
    # 审查 #7：装配路径把全部矛盾对一次交给 issue_many（一次短事务），不逐对提交
    calls = []

    def fake_issue_many(conn_, us, uid, pairs, ttl_days=7, *, commit=True):
        calls.append((list(pairs), ttl_days, commit))
        return [f"q{i}" for i in range(len(pairs))]

    monkeypatch.setattr(assembler_mod, "_clarify_issues", fake_issue_many)
    pack = assemble(conn, None, "w", "u1", budget=2000)
    assert len(calls) == 1
    pairs, _ttl, commit = calls[0]
    assert len(pairs) == 1 and pairs[0][0]  # 一对矛盾标记
    assert commit is True
    assert pack["clarification_needed"][0]["question"] == "q0"


def test_current_scope_limits_must_have(conn):
    _entry(conn, "taboo", "excel", "全局禁忌", scope="global")
    _entry(conn, "taboo", "excel", "工作项目禁忌", scope="project:work")
    _entry(conn, "taboo", "excel", "其他项目禁忌", scope="project:other")
    conn.commit()
    pack = assemble(conn, None, "w", "u1", budget=2000,
                    current_scope="project:work")
    ids = {e["id"] for e in pack["entries"]}
    assert "taboo_excel@global" in ids
    assert "taboo_excel@project:work" in ids
    assert "taboo_excel@project:other" not in ids


def test_merge_task_sink_persists_single_open(conn, tmp_path):
    from pismor.serving.merge_task import DbMergeTaskSink
    for i in range(50):
        _entry(conn, "taboo", f"t{i}", "字" * 30, conf=0.5)
    _entry(conn, "identity", "role", "用户是后端工程师")
    conn.commit()
    sink = DbMergeTaskSink(tmp_path / "test.db", "w", "u1", "project:work")
    pack = assemble(conn, None, "w", "u1", budget=2000,
                    current_scope="project:work", audit_sink=sink)
    assert pack["merge_task"] is True
    assemble(conn, None, "w", "u1", budget=2000, audit_sink=sink)
    row = conn.execute("SELECT COUNT(*) c, MAX(taboo_count) n FROM merge_tasks"
                       " WHERE status='open'").fetchone()
    assert row["c"] == 1 and row["n"] == 50
