"""每日聚合测试：orphan 晋升/废弃/audit 队列、降级、归档、复核超期、澄清过期、时区混算。

时区一致性（计划裁决 8）：created_at/issued_at 存带 +08:00 后缀的 ISO，now 注入纯日期或
带时区 ISO，SQLite julianday 对两者均归一到 UTC 后混算——边界用例钉死不误删/不漏删。
"""
import shutil
from pathlib import Path

import pytest

from pismor.pipeline.aggregator import run_daily
from pismor.core.taxonomy import load_taxonomy

REPO = Path(__file__).resolve().parent.parent
NOW = "2026-12-24T10:00:00+08:00"


class FakeLLM:
    """记录调用的假 LLM；默认回复 merge_to 语言桶。"""

    def __init__(self, reply=None):
        self.reply = reply or {"action": "merge_to", "dimension": "preference", "topic": "lang"}
        self.calls = []

    def chat_json(self, system, user, schema):
        self.calls.append((system, user, schema))
        return self.reply


def _entry(conn, *, us="w", uid="u1", dim="preference", topic="orphan", scope="global",
           status="orphan_topic", topic_new=None, confidence=0.9,
           created="2026-12-10T09:00:00+08:00", updated=None, reinforced=None,
           body="偏好简短回复", summary="简短回复"):
    updated = updated or created
    reinforced = reinforced or created
    entry_id = f"{dim}_{topic}@{scope}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,topic_new,reinforcements)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (us, uid, dim, topic, scope, entry_id, confidence, status, body, summary,
         created, updated, reinforced, topic_new))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (entry_id, us, uid, "conversation", "s1:1", "回答短一点", "explicit"))
    return entry_id


def _seed_orphans(conn, topic_new="回复要简短", n=3, dim="preference",
                  created="2026-12-10T09:00:00+08:00", uid_base=1):
    for i in range(n):
        _entry(conn, uid=f"u{uid_base + i}", dim=dim, topic_new=topic_new, created=created)
    return conn


@pytest.fixture
def fake_llm_merge():
    return FakeLLM()


@pytest.fixture
def fake_llm_new_topic():
    return FakeLLM({"action": "new_topic", "dimension": "preference", "topic": "brevity"})


@pytest.fixture
def fake_llm_sensitive():
    return FakeLLM({"action": "merge_to", "dimension": "identity", "topic": "region"})


@pytest.fixture
def fake_llm_merge_offvocab():
    # merge_to 指向词表外 topic（拼写漂移/幻觉场景）
    return FakeLLM({"action": "merge_to", "dimension": "preference", "topic": "oddtopic"})


@pytest.fixture
def fake_llm_invalid_action():
    # action 白名单外的输出
    return FakeLLM({"action": "promote_all", "dimension": "preference", "topic": "lang"})


@pytest.fixture
def seeded_orphans_3(conn):
    return _seed_orphans(conn)


@pytest.fixture
def seeded_orphans_1(conn):
    # 65 天前入库的单行观察条目：超期未达门槛 → 废弃
    return _seed_orphans(conn, n=1, created="2026-10-20T09:00:00+08:00")


@pytest.fixture
def seeded_orphans_fresh_2(conn):
    # 2 行新鲜观察条目：未达门槛且未超期 → 继续观察
    return _seed_orphans(conn, n=2)


@pytest.fixture
def seeded_taboo_orphan(conn):
    # taboo 维度词表外候选且已超期：禁自动建桶也禁物理废弃 → 只进人工队列
    return _entry(conn, dim="taboo", topic_new="不上传代码到云端",
                  created="2026-10-20T09:00:00+08:00")


@pytest.fixture
def seeded_stale_active(conn):
    # 100 天无强化 + 低置信 → 降级；updated_at 保持近期 → 不会被同轮归档
    return _entry(conn, dim="preference", topic="verbosity", status="active",
                  confidence=0.6, created="2026-12-14T09:00:00+08:00",
                  updated="2026-12-14T09:00:00+08:00",
                  reinforced="2026-09-15T09:00:00+08:00")


@pytest.fixture
def seeded_stale_taboo(conn):
    return _entry(conn, dim="taboo", topic="tool", status="active",
                  confidence=0.6, created="2026-12-14T09:00:00+08:00",
                  updated="2026-12-14T09:00:00+08:00",
                  reinforced="2026-09-15T09:00:00+08:00",
                  body="表格类结果不用 Excel 公式", summary="禁 Excel")


@pytest.fixture
def seeded_degraded_old(conn):
    # degraded 且 180 天无任何更新 → 归档
    return _entry(conn, dim="preference", topic="format", status="degraded",
                  confidence=0.5, created="2026-05-01T09:00:00+08:00",
                  updated="2026-05-01T09:00:00+08:00",
                  reinforced="2026-05-01T09:00:00+08:00",
                  body="偏好 markdown 输出", summary="markdown")


@pytest.fixture
def seeded_old_pending_review(conn):
    return _entry(conn, dim="preference", topic="style", status="pending_review",
                  created="2026-12-04T09:00:00+08:00",
                  updated="2026-12-04T09:00:00+08:00")


@pytest.fixture
def seeded_fresh_pending_review(conn):
    return _entry(conn, dim="fact", topic="family", status="pending_review",
                  created="2026-12-17T09:00:00+08:00",
                  updated="2026-12-17T09:00:00+08:00",
                  body="女儿九月上小学", summary="女儿上学")


@pytest.fixture
def taxonomy_writable(tmp_path):
    dst = tmp_path / "taxonomy.yaml"
    shutil.copy(REPO / "config" / "taxonomy.yaml", dst)
    return load_taxonomy(dst)


def test_orphan_promoted_after_threshold(conn, seeded_orphans_3, taxonomy, fake_llm_merge):
    stats = run_daily(conn, taxonomy, fake_llm_merge, auto_promote=True, now=NOW)
    assert stats["promoted"] == 3  # 同 topic_new 复现≥3次/30天 → re-key 转 active
    assert stats["merged"] == 3
    assert conn.execute("SELECT status FROM entries WHERE status='orphan_topic'").fetchone() is None
    row = conn.execute("SELECT status, topic, dimension, id, topic_new FROM entries").fetchone()
    assert row["status"] == "active" and row["topic"] == "lang" and row["topic_new"] is None
    assert row["dimension"] == "preference" and row["id"] == "preference_lang@global"
    # 证据链 entry_id 跟随 re-key
    src = conn.execute("SELECT entry_id FROM sources").fetchone()
    assert src["entry_id"] == "preference_lang@global"


def test_orphan_new_topic_promoted_and_yaml(conn, taxonomy_writable, fake_llm_new_topic):
    _seed_orphans(conn)
    before = taxonomy_writable.path.read_text(encoding="utf-8")
    stats = run_daily(conn, taxonomy_writable, fake_llm_new_topic, auto_promote=True, now=NOW)
    assert stats["promoted"] == 3
    row = conn.execute("SELECT status, topic, dimension FROM entries").fetchone()
    assert (row["status"], row["topic"], row["dimension"]) == ("active", "brevity", "preference")
    import yaml
    raw = yaml.safe_load(taxonomy_writable.path.read_text(encoding="utf-8"))
    pref = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert "brevity" in pref["topics"]  # 词表追加
    assert len(raw["regression_cases"]) == 10  # 自动生成回归样例已追加（v2 基线 9 条）
    assert raw["regression_cases"][-1]["expect"]["dimension"] == "preference"
    assert before != taxonomy_writable.path.read_text(encoding="utf-8")


def test_orphan_new_topic_queued_when_no_auto_promote(conn, taxonomy_writable, fake_llm_new_topic):
    _seed_orphans(conn)
    before = taxonomy_writable.path.read_text(encoding="utf-8")
    stats = run_daily(conn, taxonomy_writable, fake_llm_new_topic, auto_promote=False, now=NOW)
    assert stats["promoted"] == 0
    row = conn.execute("SELECT status FROM entries").fetchone()
    assert row["status"] == "orphan_topic"  # 不转正，进人工队列
    n = conn.execute("SELECT COUNT(*) c FROM review_history WHERE actor='audit'").fetchone()["c"]
    assert n >= 1
    assert taxonomy_writable.path.read_text(encoding="utf-8") == before  # 词表不动


def test_orphan_dropped_below_threshold(conn, seeded_orphans_1, taxonomy, fake_llm_merge):
    stats = run_daily(conn, taxonomy, fake_llm_merge, auto_promote=True, now=NOW)
    assert stats["dropped_orphan"] == 1  # 超期未达门槛
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0  # 证据链级联清理


def test_orphan_fresh_below_threshold_kept(conn, seeded_orphans_fresh_2, taxonomy, fake_llm_merge):
    stats = run_daily(conn, taxonomy, fake_llm_merge, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0 and stats["dropped_orphan"] == 0  # 观察中
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE status='orphan_topic'").fetchone()["c"] == 2


def test_orphan_old_group_dropped(conn, taxonomy, fake_llm_merge):
    # 3 行全部超 30 天：窗口内零行 → 整组废弃（不再滞留观察区）
    _seed_orphans(conn, n=3, created="2026-10-01T09:00:00+08:00")
    stats = run_daily(conn, taxonomy, fake_llm_merge, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0 and stats["dropped_orphan"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


def test_orphan_stale_majority_group_kept(conn, taxonomy, fake_llm_merge):
    # 3 行中仅 1 行在窗口内：窗口口径计数 1 < 3 且未归零 → 保留等复现
    _seed_orphans(conn, n=2, created="2026-10-01T09:00:00+08:00")
    _seed_orphans(conn, n=1, uid_base=9)
    stats = run_daily(conn, taxonomy, fake_llm_merge, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0 and stats["dropped_orphan"] == 0
    c = conn.execute("SELECT COUNT(*) c FROM entries WHERE status='orphan_topic'").fetchone()["c"]
    assert c == 3


def test_taboo_orphan_never_auto_promote(conn, seeded_taboo_orphan, taxonomy, fake_llm_merge):
    run_daily(conn, taxonomy, fake_llm_merge, auto_promote=True, now=NOW)
    # taboo 词表外候选只进人工队列: status 仍 orphan_topic 且有 pending 标记（写入 review_history actor=audit）
    c = conn.execute(
        "SELECT COUNT(*) c FROM entries WHERE dimension='taboo' AND status='orphan_topic'"
    ).fetchone()["c"]
    assert c == 1
    n = conn.execute("SELECT COUNT(*) c FROM review_history WHERE actor='audit'").fetchone()["c"]
    assert n == 1


def test_merge_to_sensitive_topic_queued(conn, seeded_orphans_3, taxonomy, fake_llm_sensitive):
    stats = run_daily(conn, taxonomy, fake_llm_sensitive, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0  # 归并目标 region 挂 sensitive override → 禁自动转正
    row = conn.execute("SELECT status FROM entries").fetchone()
    assert row["status"] == "orphan_topic"
    n = conn.execute("SELECT COUNT(*) c FROM review_history WHERE actor='audit'").fetchone()["c"]
    assert n == 3


def test_merge_to_offvocab_topic_queued(conn, seeded_orphans_3, taxonomy_writable,
                                        fake_llm_merge_offvocab):
    # merge_to + 词表外 topic：不转正（否则 auto_promote=False 下等效绕过人工队列建桶）
    stats = run_daily(conn, taxonomy_writable, fake_llm_merge_offvocab, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0
    row = conn.execute("SELECT status FROM entries").fetchone()
    assert row["status"] == "orphan_topic"
    n = conn.execute("SELECT COUNT(*) c FROM review_history WHERE actor='audit'").fetchone()["c"]
    assert n == 3
    import yaml
    raw = yaml.safe_load(taxonomy_writable.path.read_text(encoding="utf-8"))
    pref = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert "oddtopic" not in pref["topics"]  # 词表不被污染


def test_invalid_action_queued(conn, seeded_orphans_3, taxonomy, fake_llm_invalid_action):
    # action 白名单 {merge_to, new_topic} 之外 → 整组落 audit，不转正
    stats = run_daily(conn, taxonomy, fake_llm_invalid_action, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0
    n = conn.execute("SELECT COUNT(*) c FROM review_history WHERE actor='audit'").fetchone()["c"]
    assert n == 3


def test_demote_stale(conn, seeded_stale_active, taxonomy):
    stats = run_daily(conn, taxonomy, FakeLLM([]), True, now=NOW)
    assert stats["demoted"] == 1
    assert conn.execute("SELECT status FROM entries").fetchone()["status"] == "degraded"


def test_demote_thresholds_configurable(conn, seeded_stale_active, taxonomy):
    # 默认 90 天阈值命中（100 天无强化 conf 0.6）；放宽到 120 天/0.5 后同条不降级
    stats = run_daily(conn, taxonomy, FakeLLM([]), True, now=NOW,
                      degrade_days=120, degrade_confidence=0.5)
    assert stats["demoted"] == 0
    assert conn.execute("SELECT status FROM entries").fetchone()["status"] == "active"


def test_taboo_not_demoted_by_time(conn, seeded_stale_taboo, taxonomy):
    stats = run_daily(conn, taxonomy, FakeLLM([]), True, now=NOW)
    assert stats["demoted"] == 0
    assert conn.execute("SELECT status FROM entries").fetchone()["status"] == "active"


def test_degraded_archived_after_180d(conn, seeded_degraded_old, taxonomy):
    stats = run_daily(conn, taxonomy, FakeLLM([]), True, now=NOW)
    assert stats["archived"] == 1
    row = conn.execute("SELECT status, archived_at FROM entries").fetchone()
    assert row["status"] == "archived" and row["archived_at"] == NOW


def test_review_expired(conn, seeded_old_pending_review, seeded_fresh_pending_review, taxonomy):
    stats = run_daily(conn, taxonomy, FakeLLM([]), True, now=NOW)
    assert stats["dropped_review"] == 1  # 20 天前的待复核废弃；7 天前的保留
    n = conn.execute(
        "SELECT COUNT(*) c FROM review_history WHERE verdict='expired' AND actor='audit'"
    ).fetchone()["c"]
    assert n == 1
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE status='pending_review'"
                        ).fetchone()["c"] == 1


def test_clarification_expiry_timezone_boundary(conn, taxonomy):
    # issued_at 带 +08:00（= UTC 2026-12-20T00:00），TTL 7 天到期 = UTC 12-27T00:00
    conn.execute(
        "INSERT INTO pending_clarifications (user_source,user_id,conflicting_ids,question,issued_at)"
        " VALUES ('w','u1','[\"a\",\"b\"]','以哪种为准?','2026-12-20T08:00:00+08:00')")
    conn.commit()
    # now = UTC 2026-12-26T23:59 → 差 6.999 天，未过期不删
    s1 = run_daily(conn, taxonomy, FakeLLM([]), True, now="2026-12-27T07:59:00+08:00")
    assert s1["expired_clarifications"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 1
    # now = UTC 2026-12-27T00:01 → 差 7.001 天，过期删除
    s2 = run_daily(conn, taxonomy, FakeLLM([]), True, now="2026-12-27T08:01:00+08:00")
    assert s2["expired_clarifications"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 0


def test_demote_timezone_consistency(conn, taxonomy):
    # last_reinforced_at 两种时区写法（+08:00 与 +00:00）与纯日期 now 混算，均判 >90 天
    _entry(conn, uid="u1", topic="verbosity", status="active", confidence=0.6,
           created="2026-12-14T09:00:00+08:00", updated="2026-12-14T09:00:00+08:00",
           reinforced="2026-09-14T02:00:00+08:00")
    _entry(conn, uid="u2", topic="style", status="active", confidence=0.6,
           created="2026-12-14T09:00:00+08:00", updated="2026-12-14T09:00:00+08:00",
           reinforced="2026-09-20T00:00:00+00:00")
    stats = run_daily(conn, taxonomy, FakeLLM([]), True, now="2026-12-24")
    assert stats["demoted"] == 2
    c = conn.execute("SELECT COUNT(*) c FROM entries WHERE status='degraded'").fetchone()["c"]
    assert c == 2


# ---- fix wave 新增：orphan 组禁止建 taboo 桶 / 多 topic_new 不合并 ----

def test_orphan_group_never_creates_taboo_bucket(conn, taxonomy_writable):
    _seed_orphans(conn)
    before = taxonomy_writable.path.read_text(encoding="utf-8")
    llm = FakeLLM({"action": "new_topic", "dimension": "taboo", "topic": "weirdtool"})
    stats = run_daily(conn, taxonomy_writable, llm, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0
    assert taxonomy_writable.path.read_text(encoding="utf-8") == before  # 词表未污染
    assert conn.execute("SELECT COUNT(*) c FROM entries WHERE status='orphan_topic'"
                        ).fetchone()["c"] == 3
    notes = [r["note"] for r in conn.execute(
        "SELECT note FROM review_history WHERE actor='audit'").fetchall()]
    assert any("taboo_new_topic_queued" in n for n in notes)


def test_same_user_multiple_topic_new_not_merged(conn, taxonomy, fake_llm_merge):
    # I-1：同用户不同 topic_new 的观察条目各自成组，不压成一行
    for i, tn in enumerate(("多肉养护", "茶叶品鉴", "小说创作")):
        _entry(conn, uid="u1", topic=f"orphan{i}", topic_new=tn)
    stats = run_daily(conn, taxonomy, fake_llm_merge, auto_promote=True, now=NOW)
    assert stats["promoted"] == 0 and stats["dropped_orphan"] == 0
    rows = conn.execute("SELECT topic_new FROM entries WHERE status='orphan_topic'").fetchall()
    assert {r["topic_new"] for r in rows} == {"多肉养护", "茶叶品鉴", "小说创作"}


# ---- fix wave 新增：审计队列幂等 / 词表落盘与内存一致性 / 日批单事务 ----

def test_audit_queue_idempotent_across_runs(conn, seeded_orphans_3, taxonomy,
                                            fake_llm_invalid_action):
    # 同一批仍未处置的 orphan 组：第二轮不再调 LLM，也不再堆 audit 行
    run_daily(conn, taxonomy, fake_llm_invalid_action, auto_promote=True, now=NOW)
    n1 = conn.execute("SELECT COUNT(*) c FROM review_history WHERE actor='audit'"
                      ).fetchone()["c"]
    calls1 = len(fake_llm_invalid_action.calls)
    assert n1 == 3 and calls1 == 1
    run_daily(conn, taxonomy, fake_llm_invalid_action, auto_promote=True, now=NOW)
    n2 = conn.execute("SELECT COUNT(*) c FROM review_history WHERE actor='audit'"
                      ).fetchone()["c"]
    assert n2 == n1
    assert len(fake_llm_invalid_action.calls) == calls1


def test_taxonomy_yaml_not_written_when_transaction_fails(conn, taxonomy_writable,
                                                          fake_llm_new_topic, monkeypatch):
    # 写盘必须在事务提交之后：事务回滚时文件保持原样，内存追加也要撤掉
    from pismor.pipeline import aggregator

    _seed_orphans(conn)
    conn.commit()  # 种子先落库：本用例要验证的是"日批事务整体回滚"
    before = taxonomy_writable.path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(aggregator, "_rekey", boom)
    with pytest.raises(RuntimeError):
        run_daily(conn, taxonomy_writable, fake_llm_new_topic, auto_promote=True, now=NOW)
    assert taxonomy_writable.path.read_text(encoding="utf-8") == before
    assert "brevity" not in taxonomy_writable.dimensions["preference"].topics
    left = conn.execute("SELECT COUNT(*) c FROM entries WHERE status='orphan_topic'"
                        ).fetchone()["c"]
    assert left == 3  # 事务整体回滚


def test_orphan_new_topic_syncs_memory_topics(conn, taxonomy_writable, fake_llm_new_topic):
    # 同轮内后续组合并到刚建的新 topic 时，内存词表必须已包含它
    _seed_orphans(conn)
    run_daily(conn, taxonomy_writable, fake_llm_new_topic, auto_promote=True, now=NOW)
    assert "brevity" in taxonomy_writable.dimensions["preference"].topics


def test_run_daily_keeps_expire_in_same_transaction(conn, taxonomy, monkeypatch):
    # 日批是单事务：澄清过期不得自己提前 commit
    from pismor.pipeline import clarifier as clarifier_mod

    seen = {}
    real = clarifier_mod.expire

    def spy(conn_, now_, **kwargs):
        seen.update(kwargs)
        return real(conn_, now_, **kwargs)

    monkeypatch.setattr(clarifier_mod, "expire", spy)
    run_daily(conn, taxonomy, FakeLLM([]), auto_promote=True, now=NOW)
    assert seen.get("commit") is False
