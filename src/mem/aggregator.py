"""每日聚合（spec §8.2/§10/§12/§11）：orphan 观察区晋升/废弃、降级、归档、复核超期、澄清过期。

实现要点（上游裁决）：
- 所有扫描 WHERE 按 status 语义自然排除 '~' 前缀轨迹行：轨迹行恒 archived，任一扫描条件
  （orphan_topic/active/degraded/pending_review）都不命中；'+' 冲突行落 degraded/active，同理不命中。
- 时间统一以注入 now 参数与 SQLite julianday 混算：julianday 对带时区后缀（+08:00）与
  纯日期的 ISO 字符串均归一到 UTC，混算安全——tests/test_aggregator.py 边界用例钉死。
- orphan 聚合（30 天窗口统一口径，修复轮 1 裁决）：GROUP BY topic_new；窗口内（created_at
  >= now-30d）行数 >=3 → 归并判定；窗口内零行 → 整组物理删除（taboo 组除外，只进人工队列）；
  窗口内 1~2 行 → 保留等复现。
- 归并判定 LLM 输出 {action: merge_to|new_topic, dimension, topic}；merge_to 直接 re-key 转
  active（dimension/topic 按 LLM 指定、清 topic_new、id 与 sources.entry_id 同步跟随）；
  new_topic 且 auto_promote → 追加 taxonomy.yaml topics + 自动生成回归样例（PyYAML 整体重
  dump，注释丢失可接受，注释价值在 git 历史）再转正；auto_promote=False 或 taboo/sensitive
  → 逐行写 review_history(actor='audit') 不转正。
"""
import yaml

from mem.prompts import ORPHAN_MERGE_SYSTEM, ORPHAN_MERGE_USER

_MERGE_SCHEMA = {
    "name": "orphan_merge_verdict",
    "schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["merge_to", "new_topic"]},
            "dimension": {"type": "string"},
            "topic": {"type": "string"},
        },
        "required": ["action", "dimension", "topic"],
    },
}


def run_daily(conn, taxonomy, llm, auto_promote: bool, now: str) -> dict:
    """跑全部日批扫描，返回统计计数。"""
    stats = {"promoted": 0, "merged": 0, "demoted": 0, "archived": 0,
             "dropped_review": 0, "expired_clarifications": 0, "dropped_orphan": 0}
    with conn:
        _promote_orphans(conn, taxonomy, llm, auto_promote, now, stats)
        stats["demoted"] = _demote(conn, now)
        stats["archived"] = _archive(conn, now)
        stats["dropped_review"] = _drop_expired_reviews(conn, now)
        from mem.clarifier import expire
        stats["expired_clarifications"] = expire(conn, now)
    return stats


def _fetch_groups(conn, now):
    """观察区分组（30 天窗口统一口径）：窗口内行数>=3 → 晋升判定；窗口内零行 → 整组废弃；
    窗口内 1~2 行 → 保留等复现（消除"COUNT>=3 且 MIN 超窗"的滞留不变集）。"""
    rows = conn.execute(
        "SELECT topic_new,"
        " SUM(CASE WHEN julianday(created_at) >= julianday(?) - 30 THEN 1 ELSE 0 END) AS recent"
        " FROM entries WHERE status='orphan_topic' AND topic_new IS NOT NULL"
        " GROUP BY topic_new", (now,)).fetchall()
    promote = [r["topic_new"] for r in rows if r["recent"] >= 3]
    drop = [r["topic_new"] for r in rows if r["recent"] == 0]
    return promote, drop


def _group_rows(conn, topic_new):
    return conn.execute(
        "SELECT * FROM entries WHERE status='orphan_topic' AND topic_new=?",
        (topic_new,)).fetchall()


def _promote_orphans(conn, taxonomy, llm, auto_promote, now, stats):
    promote_groups, drop_groups = _fetch_groups(conn, now)
    for topic_new in drop_groups:
        rows = _group_rows(conn, topic_new)
        if any(r["dimension"] == "taboo" for r in rows):
            _queue_audit(conn, rows, now, note=f"taboo_orphan_expired: {topic_new}")
            continue  # taboo 词表外候选禁自动处置（不废弃、不建桶）
        _physically_drop(conn, rows)
        stats["dropped_orphan"] += len(rows)
    for topic_new in promote_groups:
        rows = _group_rows(conn, topic_new)
        if any(r["dimension"] == "taboo" for r in rows):
            _queue_audit(conn, rows, now, note=f"taboo_orphan: {topic_new}")
            continue
        block = "\n".join(f"- {d.key}({d.name}): {', '.join(d.topics)}"
                          for d in taxonomy.dimensions.values())
        summaries = "；".join(f"{r['summary']}" for r in rows[:5])
        verdict = llm.chat_json(ORPHAN_MERGE_SYSTEM, ORPHAN_MERGE_USER.format(
            topic_new=topic_new, summaries=summaries, dimension_block=block), _MERGE_SCHEMA)
        dim, topic = verdict.get("dimension"), verdict.get("topic")
        if verdict.get("action", "").startswith("merge_to") and not topic:
            topic = verdict.get("action").split(":", 1)[1]  # 兼容 merge_to:<topic> 字面格式
        if not dim or not topic or dim not in taxonomy.dimensions:
            _queue_audit(conn, rows, now, note=f"invalid_verdict: {verdict}")
            continue
        if _topic_is_sensitive(taxonomy.dimensions[dim], topic):
            _queue_audit(conn, rows, now, note=f"sensitive_topic: {dim}/{topic}")
            continue
        if verdict.get("action").startswith("new_topic"):
            if not auto_promote:
                _queue_audit(conn, rows, now, note=f"new_topic_queued: {dim}/{topic}")
                continue
            _append_taxonomy(taxonomy, dim, topic, rows)
        n = _rekey(conn, rows, dim, topic, now)
        stats["promoted"] += n
        if verdict.get("action").startswith("merge_to"):
            stats["merged"] += n


def _topic_is_sensitive(dim, topic):
    overrides = dim.topic_overrides or {}
    return bool(overrides.get(topic, {}).get("sensitive"))


def _rekey(conn, rows, dim, topic, now):
    """整组 re-key 转正：dimension/topic 按 LLM 指定，id 与证据键同步跟随；目标键被占的行进人工队列。"""
    promoted = 0
    for r in rows:
        new_id = f"{dim}_{topic}"
        exists = conn.execute(
            "SELECT 1 FROM entries WHERE user_source=? AND user_id=? AND dimension=?"
            " AND topic=? AND scope=?",
            (r["user_source"], r["user_id"], dim, topic, r["scope"])).fetchone()
        if exists:
            _queue_audit(conn, [r], now, note=f"merge_key_conflict: {new_id}")
            continue
        conn.execute(
            "UPDATE entries SET dimension=?, topic=?, id=?, status='active', topic_new=NULL,"
            " updated_at=? WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
            " AND scope=?",
            (dim, topic, new_id, now, r["user_source"], r["user_id"], r["dimension"],
             r["topic"], r["scope"]))
        conn.execute(
            "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
            (f"{new_id}@{r['scope']}", r["user_source"], r["user_id"],
             f"{r['id']}@{r['scope']}"))
        promoted += 1
    return promoted


def _queue_audit(conn, rows, now, note):
    for r in rows:
        conn.execute(
            "INSERT INTO review_history (entry_id, user_source, user_id, verdict, actor,"
            " ts, note, entry_version) VALUES (?, ?, ?, 'audit_pending', 'audit', ?, ?, ?)",
            (r["id"], r["user_source"], r["user_id"], now, note, r["updated_at"]))


def _physically_drop(conn, rows):
    for r in rows:
        conn.execute(
            "DELETE FROM sources WHERE user_source=? AND user_id=? AND entry_id=?",
            (r["user_source"], r["user_id"], f"{r['id']}@{r['scope']}"))
        conn.execute(
            "DELETE FROM entries WHERE user_source=? AND user_id=? AND dimension=?"
            " AND topic=? AND scope=?",
            (r["user_source"], r["user_id"], r["dimension"], r["topic"], r["scope"]))


def _append_taxonomy(taxonomy, dim, topic, rows):
    """词表追加 topic + 自动生成回归样例；PyYAML 整体重 dump（注释丢失可接受）。"""
    raw = yaml.safe_load(taxonomy.path.read_text(encoding="utf-8"))
    for d in raw["dimensions"]:
        if d["key"] == dim and topic not in d["topics"]:
            d["topics"].append(topic)
    raw.setdefault("regression_cases", []).append({
        "quote": rows[0]["body"],
        "expect": {"dimension": dim, "topic": topic, "scope": rows[0]["scope"], "kind": "inferred"},
    })
    taxonomy.path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _demote(conn, now):
    """90 天无强化且 conf<0.7 的 active 条目降级（taboo 恒不时间衰减，spec §11）。"""
    cur = conn.execute(
        "UPDATE entries SET status='degraded', degraded_reason='stale'"
        " WHERE status='active' AND dimension!='taboo' AND confidence<0.7"
        "   AND julianday(last_reinforced_at) < julianday(?) - 90",
        (now,))
    return cur.rowcount


def _archive(conn, now):
    """degraded 180 天无任何更新 → 归档（降级 UPDATE 不刷 updated_at，观察期自 last write 起算）。"""
    cur = conn.execute(
        "UPDATE entries SET status='archived', archived_at=?"
        " WHERE status='degraded' AND julianday(updated_at) < julianday(?) - 180",
        (now, now))
    return cur.rowcount


def _drop_expired_reviews(conn, now):
    """pending_review 超 14 天 → 物理删除（行+证据链）并写 review_history 留痕。"""
    rows = conn.execute(
        "SELECT * FROM entries WHERE status='pending_review'"
        "   AND julianday(created_at) < julianday(?) - 14", (now,)).fetchall()
    _physically_drop(conn, rows)
    for r in rows:
        conn.execute(
            "INSERT INTO review_history (entry_id, user_source, user_id, verdict, actor,"
            " ts, note, entry_version) VALUES (?, ?, ?, 'expired', 'audit', ?, NULL, ?)",
            (r["id"], r["user_source"], r["user_id"], now, r["updated_at"]))
    return len(rows)
