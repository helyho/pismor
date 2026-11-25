"""事件管线编排：澄清回流 → 触发分级 → 抽取 → 置信闸门 → 决策 → 写入（spec §10，intake 在 API 层先行）。

编排层职责（计划裁决）：
- conversation 事件开头查 pending_clarifications（clarifier.match_incoming 命中 →
  回答先经 extractor.extract_clarification 规范化，再按 UPDATE / scope 拆分 /
  单侧降级收敛矛盾对；text 事件不参与澄清关联，直通抽取）。
- 触发分级（spec §10 成本控制）：conversation 未命中变化检测 → 不调抽取 LLM，
  按话题词面命中 top-20 条目 reinforcements+1 / last_reinforced_at=now（M-7）。
- 置信闸门 review 档在本层落 pending_review 行进复核队列。
- 同 key 二次写入的 sqlite3.IntegrityError 与 IllegalTransition 在本层逐条隔离：
  单条失败不整批回滚，失败写 review_history 留痕后继续（I-2）。
- 澄清 pending 删除延后到写入成功之后（clarifier.consume）；写入失败/无候选保留 pending（I-17）。
- 任何异常只记日志不上抛：后台任务失败不得破坏 POST /events 的 202 语义；
  失败时撤销 intake 的 events_seen 标记并返回 False，调用方重发同一事件即可重放（审查 #1）。
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone

from pismor.pipeline import clarifier, extractor, gate, resolver, trigger, writer
from pismor.serving import assembler
from pismor.core import fts
from pismor.core.config import Settings
from pismor.llm.embedder import Embedder
from pismor.core.keys import entry_key, valid_scope
from pismor.llm.client import LLMClient
from pismor.core.schemas import canonical_hash
from pismor.core.taxonomy import load_taxonomy

logger = logging.getLogger("mem.pipeline")

_PENDING_COLS = ("user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,"
                 "created_at,updated_at,last_reinforced_at,degraded_reason,superseded_by,"
                 "topic_new,conflict_with,reinforcements,archived_at")


def run_pipeline(conn, settings: Settings, event: dict, *, taxonomy=None, llm=None,
                 embedder=None) -> bool:
    """后台执行完整管线；依赖缺省时按 settings 构造真实客户端（测试注入 fake）。

    返回是否成功：失败时撤销 intake 的 events_seen 标记（重发同一事件可重放），不冒泡。
    """
    try:
        taxonomy = taxonomy or load_taxonomy(settings.taxonomy_path)
        llm = llm or LLMClient(settings.llm)
        if embedder is None:
            embedder = Embedder(settings.embedding) if settings.embedding.enabled else None
        _run(conn, settings, event, taxonomy, llm, embedder)
    except Exception:  # 后台任务兜底：失败只留日志，不冒泡到上报方
        logger.exception("pipeline failed for event %s", event.get("event_id"))
        _forget_event(conn, event)
        return False
    return True


def _forget_event(conn, event):
    """撤销"已见"标记：LLM/嵌入等瞬时故障后可重放（审查 #1）。"""
    event_id, source = event.get("event_id"), event.get("source")
    try:
        conn.rollback()
        conn.execute("DELETE FROM events_seen WHERE source=? AND event_id=?", (source, event_id))
        conn.commit()
    except sqlite3.Error:  # 撤销失败只留日志，避免二次故障冒泡
        logger.exception("failed to clear seen marker for event %s", event_id)


def _run(conn, settings: Settings, event, taxonomy, llm, embedder) -> None:
    us, uid = event["source"], event["user_ref"]
    if conn.execute("SELECT 1 FROM tombstones WHERE user_source=? AND user_id=?",
                    (us, uid)).fetchone():
        # 删除权（spec §16#4）：tombstone 为永久口径——删除后不再收集新画像，也不调 LLM
        logger.info("pipeline skip for deleted user %s/%s", us, uid)
        return
    now = event.get("ts") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    origin_type = event.get("event_type") or "text"
    origin_ref = event.get("origin_ref") or event.get("event_id") or canonical_hash(event)
    known_ids = [r["id"] for r in conn.execute(
        "SELECT DISTINCT id FROM entries WHERE user_source=? AND user_id=?"
        " AND status IN ('active','degraded') LIMIT ?",
        (us, uid, settings.pipeline.known_ids_limit)).fetchall()]

    if origin_type == "conversation":
        # 澄清回流仅作用于 conversation（spec §10/§11），先于触发分级
        text = _last_user_text(event)
        if text:
            hit = clarifier.match_incoming(conn, us, uid, text, llm)
            if hit:
                _apply_clarification(conn, us, uid, hit, taxonomy, llm, embedder,
                                     origin_type, origin_ref, now,
                                     max_body=settings.pipeline.max_body_chars)
                return
        do_extract, reason = trigger.should_extract(
            event, has_history=bool(known_ids),
            long_message=settings.pipeline.trigger_long_message,
            explicit_re=trigger.build_explicit_re(
                settings.pipeline.trigger_explicit_keywords))
        if not do_extract:
            # spec §10：无变化轮次按话题词面命中累加强化，不调 LLM（M-7）
            n = _reinforce(conn, us, uid, text, now,
                           top_k=settings.pipeline.reinforce_top_k)
            logger.info("pipeline skip extraction for event %s: %s (reinforced %d)",
                        event.get("event_id"), reason, n)
            return

    candidates = extractor.extract(event, taxonomy, llm, known_ids,
                                   max_body=settings.pipeline.max_body_chars)
    auto, review, _dropped = gate.gate(candidates, **_gate_kwargs(settings))
    _write_pending(conn, us, uid, review, origin_type, origin_ref, now)
    actions = resolver.resolve(conn, us, uid, auto, llm)
    _apply(conn, us, uid, actions, embedder, origin_type, origin_ref, now)


def _gate_kwargs(settings: Settings) -> dict:
    g = settings.gate
    return {"auto_min": g.auto_min, "review_min": g.review_min,
            "inferred_cap": g.inferred_cap}


def _last_user_text(event: dict) -> str:
    messages = event.get("messages") or []
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("text"):
            return m["text"]
    return ""


def _reinforce(conn, us, uid, text, now, *, top_k: int = 20) -> int:
    """无变化轮次强化：话题 BM25 top-k 条目 +1，刷新 last_reinforced_at（M-7）。

    词面命中由 mem_fts MATCH（显式 OR，任一 token 命中即候选）召回，按 BM25
    相关度排序截断——替代旧 LIKE OR 的表扫描+无序截断：高频通用词被 IDF 降权，
    长文本偏置被长度归一化抵消，reinforcements 累积为真实相关度信号。
    mem_fts 未建表（旧库未升级）时 sqlite3.OperationalError 由编排层捕获降级。
    """
    toks = assembler.tokens(text)
    if not toks:
        return 0
    rows = conn.execute(
        "SELECT e.dimension, e.topic, e.scope FROM entries e JOIN mem_fts f"
        " ON f.rowid = e.rowid WHERE mem_fts MATCH ?"
        " AND e.user_source=? AND e.user_id=? AND e.status IN ('active','degraded')"
        " ORDER BY bm25(mem_fts) LIMIT ?",
        [fts.match_expr(toks), us, uid, top_k]).fetchall()
    if not rows:
        return 0
    with conn:
        for r in rows:
            conn.execute(
                "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=?"
                " WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
                (now, us, uid, r["dimension"], r["topic"], r["scope"]))
    return len(rows)


def _clarify_pair_rows(conn, us, uid, conflicting_ids) -> list:
    """解析 pending 的矛盾对为存活行；裸 id（旧数据）按现有行补全 scope。"""
    rows, seen = [], set()
    for value in conflicting_ids:
        # 行键即完整标记 {dim}_{topic}@{scope}；旧裸 id 值同样能匹配旧行
        found = conn.execute(
            "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=?"
            " AND status IN ('active','degraded')", (us, uid, str(value))).fetchall()
        for r in found:
            key = (r["dimension"], r["topic"], r["scope"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
    # 扩展对端：行上的 conflict_with 标记（精确解析）指向的存活行
    for r in list(rows):
        try:
            marks = json.loads(r["conflict_with"]) if r["conflict_with"] else []
        except (TypeError, ValueError):
            marks = []
        for m in marks:
            other = conn.execute(
                "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=?"
                " AND status IN ('active','degraded')", (us, uid, str(m))).fetchone()
            if other is not None:
                key = (other["dimension"], other["topic"], other["scope"])
                if key not in seen:
                    seen.add(key)
                    rows.append(other)
    return rows


def _apply_clarification(conn, us, uid, hit, taxonomy, llm, embedder,
                         origin_type, origin_ref, now, *, max_body: int = 200) -> None:
    """澄清回答闭环（C-2）：抽取/规范化 → UPDATE / scope 拆分 / 单侧降级。

    原文祈使句不入库；body/summary 来自 LLM 抽取。写入成功后才删 pending；
    无候选或全部写入失败时保留 pending（I-17）。
    """
    pair_rows = _clarify_pair_rows(conn, us, uid, hit["conflicting_ids"])
    answer = hit["answer"]
    candidates = extractor.extract_clarification(answer, taxonomy, llm, pair_rows,
                                                 max_body=max_body)
    actions = []
    for c in candidates:
        existing = conn.execute(
            "SELECT status FROM entries WHERE user_source=? AND user_id=? AND dimension=?"
            " AND topic=? AND scope=?", (us, uid, c["dimension"], c["topic"], c["scope"])).fetchone()
        if existing is None:
            actions.append({"op": "ADD", "candidate": c})
        elif existing["status"] in ("active", "degraded", "pending_review"):
            actions.append({"op": "UPDATE", "candidate": c,
                            "target": (c["dimension"], c["topic"], c["scope"])})
        # archived 同键不可复活（spec §6 非法迁移）：跳过该候选
    applied = _apply(conn, us, uid, actions, embedder, origin_type, origin_ref, now) if actions else []
    if not applied:
        logger.info("clarification produced no writable candidates; pending kept")
        return
    applied_keys = {(a["candidate"]["dimension"], a["candidate"]["topic"],
                     a["candidate"]["scope"]) for a in applied}
    _settle_pair(conn, us, uid, pair_rows, applied_keys, now)
    clarifier.consume(conn, us, uid, hit["conflicting_ids"])


def _settle_pair(conn, us, uid, pair_rows, applied_keys, now) -> None:
    """矛盾对收敛：+ 变形行归档；未被回答覆盖的规范行单侧降级；清残留互标。"""
    with conn:
        for old in pair_rows:
            row = conn.execute(
                "SELECT id, dimension, topic, scope, status FROM entries"
                " WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
                (us, uid, old["dimension"], old["topic"], old["scope"])).fetchone()
            if row is None:
                continue
            key = (row["dimension"], row["topic"], row["scope"])
            if row["topic"].startswith("+"):
                # 冲突对方（+ 变形行）不再是活跃画像：归档为轨迹（不物理删除）
                conn.execute(
                    "UPDATE entries SET status='archived', archived_at=?, conflict_with=NULL,"
                    " degraded_reason=NULL, updated_at=? WHERE user_source=? AND user_id=?"
                    " AND dimension=? AND topic=? AND scope=? AND status IN ('active','degraded')",
                    (now, now, us, uid, *key))
            elif row["dimension"] != "taboo" and key not in applied_keys:
                # 单侧降级：回答揭示该作用域不再成立；taboo 恒胜，不因澄清被降级
                conn.execute(
                    "UPDATE entries SET conflict_with=NULL, updated_at=?,"
                    " status='degraded', degraded_reason='conflict_lost'"
                    " WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?"
                    " AND status='active'",
                    (now, us, uid, *key))
            # 其余（被回答覆盖/已降级/taboo 侧）只清残留互标
            conn.execute(
                "UPDATE entries SET conflict_with=NULL, updated_at=? WHERE user_source=?"
                " AND user_id=? AND dimension=? AND topic=? AND scope=?"
                " AND conflict_with IS NOT NULL", (now, us, uid, *key))
            writer.resolve_conflicts(conn, us, uid, row["id"],
                                     row["scope"], now, revive=True, clear_pending=False)


def _write_pending(conn, us, uid, candidates, origin_type, origin_ref, now) -> int:
    """review 档候选落 pending_review 行（复核队列入口）；同 key 已有行则跳过。

    orphan 候选（dimension='orphan'，status='orphan_topic'）经 inferred cap 后恒落
    本桶：复核 approve 由 API 按 dimension='orphan' 转 orphan_topic 进观察区（spec §8.2）。
    作用域违规候选丢弃（spec §8 白名单）。
    """
    n = 0
    for c in candidates:
        if not c.get("quote"):
            continue  # 硬约束（spec §5）：无 quote 不落库
        if not valid_scope(c.get("scope")):
            logger.warning("drop pending candidate %s/%s: invalid scope %r",
                           c.get("dimension"), c.get("topic"), c.get("scope"))
            continue
        try:
            with conn:
                entry_id = entry_key(c["dimension"], c["topic"], c["scope"])
                conn.execute(
                    f"INSERT INTO entries ({_PENDING_COLS})"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (us, uid, c["dimension"], c["topic"], c["scope"], entry_id,
                     c["confidence"], "pending_review", c["body"], c["summary"] or "",
                     now, now, now, None, None, c.get("topic_new"), None, 0, None))
                conn.execute(
                    "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,"
                    " quote,kind) VALUES (?,?,?,?,?,?,?)",
                    (entry_id, us, uid, origin_type, origin_ref,
                     c["quote"], c.get("kind") or "explicit"))
            n += 1
        except sqlite3.IntegrityError:
            pass  # 同 key 重复候选：跳过（编排层降级，复核队列不重复排队）
    return n


def _apply(conn, us, uid, actions, embedder, origin_type, origin_ref, now) -> list[dict]:
    """整批写入；唯一约束/非法迁移 → 回滚后逐条重放，失败条留痕跳过。

    返回真正落库的动作（由 writer 判定）：无 quote / 非法 scope 等被跳过的候选不计入
    "已应用"，避免上层误判澄清已被覆盖（审查 #17）。
    """
    if not actions:
        return []
    try:
        return writer.apply_actions(conn, us, uid, actions, embedder, origin_type, origin_ref, now)
    except (sqlite3.IntegrityError, writer.IllegalTransition) as exc:
        logger.warning("batch write failed (%s); replaying per action", exc)
        conn.rollback()
    applied = []
    for action in actions:
        try:
            if writer.apply_actions(conn, us, uid, [action], embedder,
                                    origin_type, origin_ref, now):
                applied.append(action)
        except (sqlite3.IntegrityError, writer.IllegalTransition) as exc:
            conn.rollback()  # 该候选隔离：不拖垮其余候选
            _record_apply_failure(conn, us, uid, action, exc, now)
    return applied


def _record_apply_failure(conn, us, uid, action, exc, now) -> None:
    c = action.get("candidate") or {}
    try:
        with conn:
            conn.execute(
                "INSERT INTO review_history (entry_id, user_source, user_id, verdict, actor,"
                " ts, note, entry_version) VALUES (?, ?, ?, 'apply_failed', 'pipeline', ?, ?, ?)",
                (f"{c.get('dimension')}_{c.get('topic')}@{c.get('scope')}", us, uid, now, str(exc), ""))
    except sqlite3.Error:  # 留痕失败不得再冒泡
        logger.exception("failed to record apply failure")
