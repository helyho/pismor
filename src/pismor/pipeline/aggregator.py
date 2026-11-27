"""每日聚合（spec §8.2/§10/§12/§11）：orphan 观察区启用/废弃、衰减、归档、复核超期、澄清过期。

实现要点（上游裁决）：
- 所有扫描 WHERE 按 status/variant 语义自然排除 trace 行：轨迹行恒 archived，任一扫描条件
  （orphan_topic/active/degraded/pending_review）都不命中；conflict 行按 status 参与。
- 时间统一以注入 now 参数与 SQLite julianday 混算：julianday 对带时区后缀（+08:00）与
  纯日期的 ISO 字符串均归一到 UTC，混算安全——tests/test_aggregator.py 边界用例钉死。
- orphan 聚合（30 天窗口统一口径，修复轮 1 裁决）：GROUP BY topic_new；窗口内（created_at
  >= now-30d）行数 >=3 → 归并判定；窗口内零行 → 整组物理删除（taboo 组除外，只进人工队列）；
  窗口内 1~2 行 → 保留等复现。
- 归并判定 LLM 输出 {action: merge_to|new_topic, dimension, topic}；merge_to 直接 re-key 转
  active（dimension/topic 按 LLM 指定、清 topic_new、id 与 sources.entry_id 同步跟随）；
  new_topic 且 auto_activate → 追加 taxonomy.yaml topics + 自动生成回归样例（round-trip
  写回，保留注释与键顺序）再转正；auto_activate=False 或 taboo/sensitive
  → 逐行写 review_history(actor='audit') 不转正。
"""
import json

from pismor.core.taxonomy_edit import (
    flush_taxonomy_edits,
    plan_topic_alias,
    revert_taxonomy_edits,
)


_ALIVE = (
    "NOT EXISTS (SELECT 1 FROM tombstones t"
    " WHERE t.user_source=entries.user_source AND t.user_id=entries.user_id)"
)
from pismor.llm.prompts import ORPHAN_MERGE_SYSTEM, ORPHAN_MERGE_USER

_MERGE_SCHEMA = {
    "name": "orphan_merge_decision",
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


def run_daily(conn, taxonomy, llm, auto_activate: bool, now: str, *,
              degrade_days: int = 90, degrade_confidence: float = 0.7,
              taxonomy_backup_root=None, taxonomy_backup_keep: int = 30,
              taboo_merge_threshold: int = 40,
              on_taxonomy_updated=None) -> dict:
    """跑全部日批扫描，返回统计计数。衰减阈值可由 settings.aggregator 配置。

    三段式（F17）：阶段1 只读收集孤儿处置计划、阶段2 无锁调 LLM 归并判定——两者都
    不持有写事务，LLM 慢/超时不再阻塞 serving 写路径；LLM 失败直接冒泡，本轮零写入。
    阶段3 单写事务完成全部落库（含澄清过期，故 expire 传 commit=False）；taxonomy
    追加先改内存（同轮后续组需要看到新 topic），yaml 落盘推迟到提交成功之后——
    事务回滚时文件与内存都不留痕。组行以阶段1 快照参与判定：应用时行已被并发
    删除/改键，则按主键 WHERE 自然落空。
    """
    stats = {"activated": 0, "merged": 0, "decayed": 0, "archived": 0,
             "dropped_review": 0, "expired_clarifications": 0, "dropped_orphan": 0,
             "merge_tasks": 0}
    appends: list[dict] = []
    plans, drop_plans = _collect_orphan_plans(conn, taxonomy, now)
    _judge_orphan_plans(llm, taxonomy, plans)  # 阶段2：无锁 LLM 判定，异常冒泡=零写入
    try:
        with conn:
            _apply_orphan_plans(conn, taxonomy, auto_activate, now, stats, appends,
                                plans, drop_plans)
            stats["merge_tasks"] = _open_merge_tasks(
                conn, taxonomy, now, taboo_merge_threshold)
            stats["decayed"] = _decay(conn, now, degrade_days, degrade_confidence)
            stats["archived"] = _archive(conn, now)
            stats["dropped_review"] = _drop_expired_reviews(conn, now)
            from pismor.pipeline.clarifier import expire
            stats["expired_clarifications"] = expire(conn, now, commit=False)
    except Exception:
        revert_taxonomy_edits(taxonomy, appends)
        raise
    new_taxonomy = flush_taxonomy_edits(
        taxonomy,
        appends,
        backup_root=taxonomy_backup_root,
        keep=taxonomy_backup_keep,
    )
    if new_taxonomy is not None and on_taxonomy_updated is not None:
        on_taxonomy_updated(new_taxonomy)
    return stats


def _fetch_groups(conn, now):
    """观察区分组（30 天窗口统一口径）：窗口内行数>=3 → 启用判定；窗口内零行 → 整组废弃；
    窗口内 1~2 行 → 保留等复现（消除"COUNT>=3 且 MIN 超窗"的滞留不变集）。"""
    rows = conn.execute(
        "SELECT topic_new,"
        " SUM(CASE WHEN julianday(created_at) >= julianday(?) - 30 THEN 1 ELSE 0 END) AS recent"
        f" FROM entries WHERE {_ALIVE} AND status='orphan_topic' AND topic_new IS NOT NULL"
        " GROUP BY topic_new", (now,)).fetchall()
    activate = [r["topic_new"] for r in rows if r["recent"] >= 3]
    drop = [r["topic_new"] for r in rows if r["recent"] == 0]
    return activate, drop


def _group_rows(conn, topic_new):
    return conn.execute(
        f"SELECT * FROM entries WHERE {_ALIVE} AND status='orphan_topic' AND topic_new=?",
        (topic_new,)).fetchall()


def _dimension_block(taxonomy, extra_new: dict[str, set[str]] | None = None) -> str:
    """词表块文本；extra_new 把本轮已判定的 new_topic 并入展示（同轮后续组可见，保原语义）。"""
    lines = []
    for d in taxonomy.dimensions.values():
        disabled = getattr(d, "disabled_topics", None) or []
        topics = [t for t in d.topics if t not in disabled]
        if extra_new:
            for t in sorted(extra_new.get(d.key, ())):
                if t not in topics:
                    topics.append(t)
        lines.append(f"- {d.key}({d.name}): " + ", ".join(topics))
    return "\n".join(lines)


def _collect_orphan_plans(conn, taxonomy, now):
    """阶段1（只读）：观察区分组收集处置计划，不写库不调 LLM。

    返回 (plans, drop_plans)。plans 每项 {"topic_new", "rows", ...} 进 LLM 判定
    （taboo 组标 taboo_queue，直接进人工队列不判定）；drop_plans 是窗口零复现组。
    """
    activate_groups, drop_groups = _fetch_groups(conn, now)
    drop_plans = []
    for topic_new in drop_groups:
        rows = _group_rows(conn, topic_new)
        drop_plans.append({"topic_new": topic_new, "rows": rows,
                           "taboo": any(r["dimension"] == "taboo" for r in rows)})
    plans = []
    for topic_new in activate_groups:
        rows = _group_rows(conn, topic_new)
        if any(r["dimension"] == "taboo" for r in rows):
            plans.append({"topic_new": topic_new, "rows": rows, "taboo_queue": True})
            continue
        if _already_queued(conn, rows):
            continue  # 条目状态没变且已进人工队列：不重复判定、不重复堆审计行
        plans.append({"topic_new": topic_new, "rows": rows,
                      "summaries": "；".join(f"{r['summary']}" for r in rows[:5])})
    return plans, drop_plans


def _judge_orphan_plans(llm, taxonomy, plans):
    """阶段2（无锁）：逐组调 LLM 归并判定，decision 写回 plan；异常冒泡=本轮零写入。"""
    seen_new: dict[str, set[str]] = {}
    for p in plans:
        if p.get("taboo_queue"):
            continue
        p["decision"] = llm.chat_json(
            ORPHAN_MERGE_SYSTEM,
            ORPHAN_MERGE_USER.format(topic_new=p["topic_new"], summaries=p["summaries"],
                                     dimension_block=_dimension_block(taxonomy, seen_new)),
            _MERGE_SCHEMA)
        decision = p["decision"]
        if (decision.get("action") == "new_topic" and decision.get("dimension")
                and decision.get("topic")):
            seen_new.setdefault(decision["dimension"], set()).add(decision["topic"])


def _apply_orphan_plans(conn, taxonomy, auto_activate, now, stats, appends,
                        plans, drop_plans):
    """阶段3（单写事务内调用）：应用处置计划完成全部落库。"""
    for p in drop_plans:
        if p["taboo"]:
            _queue_audit(conn, p["rows"], now,
                         note=f"taboo_orphan_expired: {p['topic_new']}")
            continue  # taboo 词表外候选禁自动处置（不废弃、不建桶）
        _physically_drop(conn, p["rows"])
        stats["dropped_orphan"] += len(p["rows"])
    for p in plans:
        if p.get("taboo_queue"):
            _queue_audit(conn, p["rows"], now, note=f"taboo_orphan: {p['topic_new']}")
            continue
        _apply_orphan_decision(conn, taxonomy, auto_activate, now, stats, appends, p)


def _apply_orphan_decision(conn, taxonomy, auto_activate, now, stats, appends, plan):
    """单组判定应用：校验链（I1 修复）→ 白名单/sensitive/taboo 禁自动 → re-key 转正。"""
    rows, decision, topic_new = plan["rows"], plan["decision"], plan["topic_new"]
    action = decision.get("action") or ""
    is_merge = action == "merge_to" or action.startswith("merge_to:")  # 兼容 merge_to:<topic> 字面格式
    is_new = action == "new_topic"
    dim, topic = decision.get("dimension"), decision.get("topic")
    if is_merge and not topic:
        topic = action.split(":", 1)[1]
    # 校验链（I1 修复）：action 白名单 → dimension 合法 → merge_to 目标必须在词表内
    #（schema 是服务端软约束，本地不校验则词表外 merge_to 在 auto_activate=False 下绕过人工队列）
    if (not is_merge and not is_new) or not dim or not topic or dim not in taxonomy.dimensions:
        _queue_audit(conn, rows, now, note=f"invalid_decision: {decision}")
        return
    if topic in (getattr(taxonomy.dimensions[dim], "disabled_topics", None) or []):
        _queue_audit(conn, rows, now, note=f"disabled_topic: {dim}/{topic}")
        return
    if is_merge and topic not in (taxonomy.dimensions[dim].topics or []):
        _queue_audit(conn, rows, now, note=f"merge_target_not_in_taxonomy: {dim}/{topic}")
        return
    if _topic_is_sensitive(taxonomy.dimensions[dim], topic):
        _queue_audit(conn, rows, now, note=f"sensitive_topic: {dim}/{topic}")
        return
    if is_new:
        if dim == "taboo":
            # taboo 禁止运行期扩充（spec §7）：orphan 组不得建成 taboo 新桶，仅进人工队列
            _queue_audit(conn, rows, now, note=f"taboo_new_topic_queued: {dim}/{topic}")
            return
        if not auto_activate:
            _queue_audit(conn, rows, now, note=f"new_topic_queued: {dim}/{topic}")
            return
        _stage_taxonomy(taxonomy, dim, topic, topic_new, rows, appends)
    n = _rekey(conn, rows, dim, topic, now)
    stats["activated"] += n
    if is_merge:
        stats["merged"] += n
        if n:
            alias_edit = plan_topic_alias(
                taxonomy, dim, _bare_topic(topic_new, taxonomy), topic)
            if alias_edit is not None:
                appends.append(alias_edit)


def _topic_is_sensitive(dim, topic):
    overrides = dim.topic_overrides or {}
    return bool(overrides.get(topic, {}).get("sensitive"))


def _rekey(conn, rows, dim, topic, now):
    """整组 re-key 转正：dimension/topic 按 LLM 指定；id 稳定不变，证据键无需迁移；目标键被占的行进人工队列。"""
    activated = 0
    for r in rows:
        exists = conn.execute(
            "SELECT 1 FROM entries WHERE user_source=? AND user_id=? AND dimension=?"
            " AND topic=? AND variant='normal' AND id<>?",
            (r["user_source"], r["user_id"], dim, topic, r["id"])).fetchone()
        if exists:
            _queue_audit(conn, [r], now, note=f"merge_key_conflict: {dim}_{topic}")
            continue
        conn.execute(
            "UPDATE entries SET dimension=?, topic=?, status='active', topic_new=NULL,"
            " updated_at=? WHERE id=?",
            (dim, topic, now, r["id"]))
        activated += 1
    return activated


def _already_queued(conn, rows) -> bool:
    """整组行都已按当前版本进过人工队列 → 本轮跳过重复判定。"""
    for r in rows:
        hit = conn.execute(
            "SELECT 1 FROM review_history WHERE entry_id=? AND user_source=? AND user_id=?"
            " AND actor='audit' AND decision='audit_pending' AND entry_version=? LIMIT 1",
            (r["id"], r["user_source"], r["user_id"], r["updated_at"])).fetchone()
        if hit is None:
            return False
    return True


def _queue_audit(conn, rows, now, note):
    """逐行入人工队列；同条目同版本同原因重复入队直接跳过（日批可重跑）。"""
    for r in rows:
        hit = conn.execute(
            "SELECT 1 FROM review_history WHERE entry_id=? AND user_source=? AND user_id=?"
            " AND actor='audit' AND decision='audit_pending' AND note=? AND entry_version=?"
            " LIMIT 1",
            (r["id"], r["user_source"], r["user_id"], note, r["updated_at"])).fetchone()
        if hit is not None:
            continue
        conn.execute(
            "INSERT INTO review_history (entry_id, user_source, user_id, decision, actor,"
            " ts, note, entry_version) VALUES (?, ?, ?, 'audit_pending', 'audit', ?, ?, ?)",
            (r["id"], r["user_source"], r["user_id"], now, note, r["updated_at"]))


def _physically_drop(conn, rows):
    for r in rows:
        conn.execute(
            "DELETE FROM sources WHERE user_source=? AND user_id=? AND entry_id=?",
            (r["user_source"], r["user_id"], r["id"]))
        conn.execute(
            "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=?",
            (r["user_source"], r["user_id"], r["id"]))


def _bare_topic(topic_new: str, taxonomy) -> str:
    """topic_new 形如 "<dim>/<裸主题>"；仅首段是已知维度时剥前缀，否则整串当别名。"""
    head, sep, tail = (topic_new or "").partition("/")
    if sep and head in taxonomy.dimensions and tail:
        return tail
    return topic_new or ""


def _stage_taxonomy(taxonomy, dim, topic, topic_new, rows, appends):
    """内存词表立即追加（同轮后续组要能 merge 到新 topic）；yaml 落盘交给 flush_taxonomy_edits。"""
    d = taxonomy.dimensions[dim]
    added = topic not in d.topics
    if added:
        d.topics.append(topic)
    append = {
        "dim": dim,
        "topics": [topic] if added else [],
        "case": {"quote": rows[0]["body"],
                 "expect": {"dimension": dim, "topic": topic, "kind": "inferred"}},
    }
    alias_edit = plan_topic_alias(taxonomy, dim, _bare_topic(topic_new, taxonomy), topic)
    if alias_edit is not None:
        append["aliases"] = alias_edit["aliases"]
    appends.append(append)
    return append


def _decay(conn, now, degrade_days: int = 90, degrade_confidence: float = 0.7):
    """degrade_days 天无强化且 conf<degrade_confidence 的 active 条目衰减（taboo 恒不时间衰减，spec §11）。"""
    cur = conn.execute(
        "UPDATE entries SET status='degraded', degraded_reason='stale'"
        " WHERE status='active' AND variant='normal' AND dimension!='taboo' AND confidence<?"
        "   AND julianday(last_reinforced_at) < julianday(?) - ?"
        f"   AND {_ALIVE}",
        (degrade_confidence, now, degrade_days))
    return cur.rowcount


def _archive(conn, now):
    """degraded 180 天无任何更新 → 归档（衰减 UPDATE 不刷 updated_at，观察期自 last write 起算）。"""
    cur = conn.execute(
        "UPDATE entries SET status='archived', archived_at=?"
        f" WHERE status='degraded' AND julianday(updated_at) < julianday(?) - 180"
        f"   AND {_ALIVE}",
        (now, now))
    return cur.rowcount


def _drop_expired_reviews(conn, now):
    """pending_review 超 14 天 → 物理删除（行+证据链）并写 review_history 留痕。"""
    rows = conn.execute(
        f"SELECT * FROM entries WHERE {_ALIVE} AND status='pending_review'"
        "   AND julianday(created_at) < julianday(?) - 14", (now,)).fetchall()
    _physically_drop(conn, rows)
    for r in rows:
        conn.execute(
            "INSERT INTO review_history (entry_id, user_source, user_id, decision, actor,"
            " ts, note, entry_version) VALUES (?, ?, ?, 'expired', 'audit', ?, NULL, ?)",
            (r["id"], r["user_source"], r["user_id"], now, r["updated_at"]))
    return len(rows)


def _open_merge_tasks(conn, taxonomy, now, threshold: int) -> int:
    """taboo 存活条数超阈值的用户 → 幂等建 open 归并任务，附自由 topic 提案。

    任务粒度 = 用户（全量汇总），与装配侧 merge_task 同阈值同语义；
    已存在 open 任务只刷新计数与提案，不重置 created_at。不做任何 entries 改动：
    实际归并走 review.decision(topic=...) 人工确认。
    """
    dim = taxonomy.dimensions.get("taboo")
    allowed = set(getattr(dim, "topics", None) or []) if dim else set()
    rows = conn.execute(
        "SELECT user_source, user_id, COUNT(*) AS n,"
        " GROUP_CONCAT(DISTINCT topic) AS topics"
        f" FROM entries WHERE {_ALIVE} AND dimension='taboo'"
        " AND status IN ('active','degraded') AND variant IN ('normal','conflict')"
        " GROUP BY user_source, user_id").fetchall()
    per_user: dict[tuple[str, str], dict] = {}
    for r in rows:
        bucket = per_user.setdefault(
            (r["user_source"], r["user_id"]), {"count": 0, "free": set()})
        bucket["count"] += r["n"]
        for t in (r["topics"] or "").split(","):
            if t and t not in allowed:
                bucket["free"].add(t)
    opened = 0
    for (us, uid), b in per_user.items():
        if b["count"] <= threshold:
            continue
        proposal = json.dumps(
            {"taboo_count": b["count"], "free_topics": sorted(b["free"]),
             "allowed_topics": sorted(allowed)}, ensure_ascii=False)
        conn.execute(
            "INSERT OR IGNORE INTO merge_tasks"
            "(user_source, user_id, taboo_count, status, proposal_json,"
            " created_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?, ?)",
            (us, uid, b["count"], proposal, now, now))
        conn.execute(
            "UPDATE merge_tasks SET taboo_count=?, proposal_json=?, updated_at=?"
            " WHERE user_source=? AND user_id=? AND status='open'",
            (b["count"], proposal, now, us, uid))
        opened += 1
    return opened
