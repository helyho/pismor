"""事件管线编排：澄清回流 → 抽取 → 置信闸门 → 决策 → 写入（spec §10，intake 在 API 层先行）。

编排层职责（计划裁决）：
- conversation 事件开头查 pending_clarifications（clarifier.match_incoming 命中 →
  kind=clarification 候选直通决策步，本人意志优先）；text 事件不参与澄清关联，直通抽取。
- 置信闸门 review 档在本层落 pending_review 行进复核队列（writer 不覆盖该状态）。
- 同 key 二次写入的 sqlite3.IntegrityError 在本层捕获：整批回滚后逐条重放，
  冲突单条降级跳过（reinforcements 逻辑已在 writer 的 NOOP 内）。
- clarify 检查不在管线内：assembler 装配时自行检测矛盾对并调 clarifier.issue。
- 任何异常只记日志不上抛：后台任务失败不得破坏 POST /events 的 202 语义。
"""
import logging
import sqlite3
from datetime import datetime, timezone

from mem import clarifier, extractor, gate, resolver, writer
from mem.config import Settings
from mem.embedder import Embedder
from mem.llm import LLMClient
from mem.taxonomy import load_taxonomy

logger = logging.getLogger("mem.pipeline")

_PENDING_COLS = ("user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,"
                 "created_at,updated_at,last_reinforced_at,degraded_reason,superseded_by,"
                 "topic_new,conflict_with,reinforcements,archived_at")


def run_pipeline(conn, settings: Settings, event: dict, *, taxonomy=None, llm=None,
                 embedder=None) -> None:
    """后台执行完整管线；依赖缺省时按 settings 构造真实客户端（测试注入 fake）。"""
    try:
        taxonomy = taxonomy or load_taxonomy(settings.taxonomy_path)
        llm = llm or LLMClient(settings.llm)
        if embedder is None:
            embedder = Embedder(settings.embedding) if settings.embedding.enabled else None
        _run(conn, event, taxonomy, llm, embedder)
    except Exception:  # 后台任务兜底：失败只留日志，不冒泡到上报方
        logger.exception("pipeline failed for event %s", event.get("event_id"))


def _run(conn, event, taxonomy, llm, embedder) -> None:
    us, uid = event["source"], event["user_ref"]
    now = event.get("ts") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    origin_type = event.get("event_type") or "text"
    origin_ref = event.get("origin_ref") or event.get("event_id") or ""

    clarify_cands: list[dict] = []
    if origin_type == "conversation":  # 澄清回流仅作用于 conversation（spec §10/§11）
        text = _last_user_text(event)
        if text:
            hit = clarifier.match_incoming(conn, us, uid, text, llm)
            if hit:
                clarify_cands = _clarify_candidates(conn, us, uid, hit)

    known_ids = [r["id"] for r in conn.execute(
        "SELECT DISTINCT id FROM entries WHERE user_source=? AND user_id=?"
        " AND status IN ('active','degraded') LIMIT 200", (us, uid)).fetchall()]
    candidates = extractor.extract(event, taxonomy, llm, known_ids)
    auto, review, _dropped = gate.gate(candidates)
    _write_pending(conn, us, uid, review, origin_type, origin_ref, now)
    actions = resolver.resolve(conn, us, uid, clarify_cands + auto, llm)
    _apply(conn, us, uid, actions, embedder, origin_type, origin_ref, now)


def _last_user_text(event: dict) -> str:
    messages = event.get("messages") or []
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("text"):
            return m["text"]
    return ""


def _clarify_candidates(conn, us, uid, hit: dict) -> list[dict]:
    """澄清回答构造为单条 clarification 候选（取矛盾对主行 key），直通决策步。"""
    answer = hit["answer"]
    for eid in hit["conflicting_ids"]:
        row = conn.execute(
            "SELECT dimension, topic, scope FROM entries"
            " WHERE user_source=? AND user_id=? AND id=? AND status IN ('active','degraded')"
            " ORDER BY updated_at DESC LIMIT 1", (us, uid, eid)).fetchone()
        if row is None:
            continue
        return [{"dimension": row["dimension"], "topic": row["topic"], "scope": row["scope"],
                 "body": answer, "summary": answer[:80], "confidence": 1.0,
                 "kind": "clarification", "quote": answer, "topic_new": None}]
    return []


def _write_pending(conn, us, uid, candidates, origin_type, origin_ref, now) -> int:
    """review 档候选落 pending_review 行（复核队列入口）；同 key 已有行则跳过。"""
    n = 0
    for c in candidates:
        if not c.get("quote"):
            continue  # 硬约束（spec §5）：无 quote 不落库
        try:
            with conn:
                entry_id = f"{c['dimension']}_{c['topic']}"
                conn.execute(
                    f"INSERT INTO entries ({_PENDING_COLS})"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (us, uid, c["dimension"], c["topic"], c["scope"], entry_id,
                     c["confidence"], "pending_review", c["body"], c["summary"] or "",
                     now, now, now, None, None, c.get("topic_new"), None, 0, None))
                conn.execute(
                    "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,"
                    " quote,kind) VALUES (?,?,?,?,?,?,?)",
                    (f"{entry_id}@{c['scope']}", us, uid, origin_type, origin_ref,
                     c["quote"], c.get("kind") or "explicit"))
            n += 1
        except sqlite3.IntegrityError:
            pass  # 同 key 重复候选：跳过（编排层降级，复核队列不重复排队）
    return n


def _apply(conn, us, uid, actions, embedder, origin_type, origin_ref, now) -> None:
    """整批写入；唯一约束冲突（同 key 二次写入）→ 回滚后逐条重放，冲突条降级跳过。"""
    try:
        writer.apply_actions(conn, us, uid, actions, embedder, origin_type, origin_ref, now)
    except sqlite3.IntegrityError:
        conn.rollback()
        for action in actions:
            try:
                writer.apply_actions(conn, us, uid, [action], embedder,
                                     origin_type, origin_ref, now)
            except sqlite3.IntegrityError:
                conn.rollback()  # 该候选降级 NOOP（编排层吞异常继续）
