"""事务写入器：Action 列表 → entries + sources + embeddings 单事务落库（spec §6/§11/§12/§20）。

形状裁决（计划裁决 5/6 + 上游裁决 a/b/c）：
- 唯一键 (user_source,user_id,dimension,topic,variant,variant_seq)；normal 每键最多 1 行，
  conflict/trace/snapshot 按 variant_seq 递增共存，topic 不再做前缀变形：
  UPDATE = 原 normal 行覆盖写入新值（status 恢复 active），旧值复制为 trace 变体
  （superseded_by 指向原 id，sources 迁到 trace），审计与装配按 variant/status 过滤。
- CONFLICT 同 key 双存：新建 conflict 变体行并双向 conflict_with；
  对方是 taboo 时 taboo 恒胜（spec §11）：taboo 行只补 conflict_with（§13 装配
  "taboo 出全句、对方曾观察"依赖双向标记），不降置信不降状态；preference 侧直接 degraded。
- 行键为稳定 id（keys.new_id）：entries.id = sources.entry_id；topic/variant 仅业务字段；
  旧裸 id 由 init_schema 迁移，跨行关联标记同形。
- pending_review 不阻塞演进：同 key 新证据先把待复核行移入 "!" 快照（仍在复核队列），
  再写入规范 active 行（spec §10/§14，I-2）。
- 引用完整性由本写入器事务保证：entries 与 sources 同事务写入。
- 向量在写事务外批量预先计算（网络 IO 不持有 WAL 写锁），库内向量按 L2 归一化后写入
  （装配侧余弦→L2 距离换算依赖该前提）；embed 抛 EmbedUnavailable/KeyError/
  JSONDecodeError（网关 200+异常体）、sqlite 写入错误（维度配置错等）或 embeddings
  表未启用 → 该条目跳过向量行，条目行照写，不抛出（spec §20）。
- 已落库动作口径：apply_actions 只返回真正写入的动作（无 quote/目标缺失等
  被跳过的候选不出现），供上层判断"澄清是否已被写入覆盖"。
"""
import json
import logging
import math
import sqlite3

from pismor.core.lifecycle import IllegalTransition, check_transition
from pismor.llm.embedder import EmbedUnavailable, l2_normalize
from pismor.core.keys import (
    VARIANT_CONFLICT, VARIANT_NORMAL, VARIANT_SNAPSHOT, VARIANT_TRACE,
    new_id,
)

logger = logging.getLogger("mem.writer")

# 合法迁移边见 core/lifecycle.py（spec §6 状态机图）；
# INSERT 任意初始态、NOOP 同态强化不走迁移校验。

_ENTRY_COLS = ("user_source,user_id,dimension,topic,id,variant,variant_seq,"
               "confidence,status,body,summary,created_at,updated_at,last_reinforced_at,"
               "degraded_reason,superseded_by,topic_new,conflict_with,reinforcements,"
               "archived_at")

# 会写入向量行的 op（与 _add/_update/_conflict 的 append 点一一对应）
_VECTOR_OPS = ("ADD", "UPDATE", "CONFLICT")

# degraded → active 的复活门槛默认值（spec §6：新强化证据 conf ≥ 0.85）。
# 只约束管线自动写入；管线按 settings.gate.auto_min 传入（与显式直通门槛联动），
# 人工复核 confirm / 澄清 revive 是裁决路径，不受此限。
REVIVE_MIN_CONFIDENCE = 0.85

# 嵌入降级口径（spec §20）：单条失败只丢该条向量
_EMBED_ERRORS = (EmbedUnavailable, KeyError, json.JSONDecodeError, IndexError, TypeError)


def _check_transition(old_status, new_status):
    check_transition("auto", old_status, new_status)


def apply_actions(conn, user_source, user_id, actions, embedder, origin_type, origin_ref, now,
                  *, revive_min_confidence: float = REVIVE_MIN_CONFIDENCE):
    """单事务应用 Action 列表。

    返回真正落库的 action 列表。嵌入在事务开始前完成（批量一次，失败逐条重试）。
    revive_min_confidence 为 degraded → active 的复活门槛（管线传 gate.auto_min）。
    """
    # taboo 优先：同批双新增时 taboo 行先落库，preference 行处理时查询可见 → 恒胜降级
    ordered = sorted(actions, key=lambda a: a.get("candidate", {}).get("dimension") != "taboo")
    precomputed = _precompute_vectors(conn, embedder, ordered)
    vectors: list[tuple[str, str]] = []  # (四元组合成键, 嵌入文本)
    applied = []
    with conn:
        for action in ordered:
            c = action.get("candidate") or {}
            if "confidence" in c:
                _conf = float(c.get("confidence") or 0)
                # F6: 防御层——NaN 使复活门槛 `conf < min` 恒 False，超界值污染装配排序
                c["confidence"] = min(1.0, max(0.0, _conf)) if math.isfinite(_conf) else 0.0
            if _apply_one(conn, user_source, user_id, action, origin_type, origin_ref, now,
                          vectors, revive_min_confidence):
                applied.append(action)
        _write_vectors(conn, user_source, user_id, vectors, precomputed)
    return applied


def _precompute_vectors(conn, embedder, actions):
    """事务外算向量：{正文: 向量}；批量一次，条数不符或失败时逐条重试以保留单条隔离。"""
    if embedder is None or not _has_embeddings_table(conn):
        return {}
    texts = []
    for action in actions:
        c = action.get("candidate") or {}
        if action.get("op") not in _VECTOR_OPS or not c.get("quote"):
            continue
        text = c.get("body")
        if text and text not in texts:
            texts.append(text)
    if not texts:
        return {}
    try:
        vecs = embedder.embed(texts)
        if len(vecs) == len(texts):
            return dict(zip(texts, vecs))
    except _EMBED_ERRORS:
        pass
    precomputed = {}
    for text in texts:
        try:
            precomputed[text] = embedder.embed([text])[0]
        except _EMBED_ERRORS:
            continue
    return precomputed


def _apply_one(conn, us, uid, action, origin_type, origin_ref, now, vectors,
               revive_min_confidence=REVIVE_MIN_CONFIDENCE) -> bool:
    op, c = action["op"], action["candidate"]
    if op == "ADD":
        return _add(conn, us, uid, c, now, origin_type, origin_ref, vectors)
    if op == "UPDATE":
        return _update(conn, us, uid, c, action["target"], now, origin_type, origin_ref, vectors,
                       revive_min_confidence)
    if op == "CONFLICT":
        return _conflict(conn, us, uid, c, action["target"], now, origin_type, origin_ref, vectors)
    if op == "NOOP":
        return _noop(conn, us, uid, action["target"], now)
    if op == "ORPHAN":
        return _orphan(conn, us, uid, c, now, origin_type, origin_ref)
    raise ValueError(f"unknown op: {op}")


def _fetch_row(conn, us, uid, key):
    """按 (dimension,topic) 取规范行；variant 行只能按 id 访问。"""
    d, t = key
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
        " AND variant=?",
        (us, uid, d, t, VARIANT_NORMAL)).fetchone()


def _find_taboo(conn, us, uid, c):
    """同 (user, topic) 的存活 taboo 规范行；taboo 候选自身不构成互斥。"""
    if c["dimension"] == "taboo":
        return None
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=?"
        " AND dimension='taboo' AND topic=? AND variant=? AND status IN ('active','degraded')",
        (us, uid, c["topic"], VARIANT_NORMAL)).fetchone()


def _add(conn, us, uid, c, now, origin_type, origin_ref, vectors) -> bool:
    if not c.get("quote"):
        return False  # 硬约束（spec §5）：无 quote 的条目拒绝入库
    taboo = _find_taboo(conn, us, uid, c)
    if taboo:
        # taboo 恒胜：preference 新行直接 degraded(conflict_lost)，taboo 行只补标记
        status, reason, confidence = "degraded", "conflict_lost", c["confidence"] * 0.8
        conflict_ids = [taboo["id"]]
    else:
        status, reason, confidence = "active", None, c["confidence"]
        conflict_ids = None
    entry_id = _insert_entry(conn, us, uid, c, status=status, now=now, origin_type=origin_type,
                             origin_ref=origin_ref, confidence=confidence,
                             degraded_reason=reason, conflict_with=conflict_ids)
    if entry_id:
        vectors.append((entry_id, c["body"]))
        if taboo:
            _append_conflict_mark(conn, taboo, entry_id)
    return bool(entry_id)


def _update(conn, us, uid, c, target, now, origin_type, origin_ref, vectors,
            revive_min_confidence=REVIVE_MIN_CONFIDENCE) -> bool:
    if not c.get("quote"):
        return False
    if target is None:
        raise IllegalTransition("UPDATE requires target")
    d, t = target
    old = _fetch_row(conn, us, uid, target)
    if old is None:
        raise IllegalTransition(f"UPDATE target not found: {target}")
    if old["status"] == "pending_review":
        # 待复核行不阻塞演进：移入 "!" 快照继续留在队列，规范键写入新 active 行
        _release_pending(conn, us, uid, old, now)
        entry_id = _insert_entry(conn, us, uid, c, status="active", now=now,
                                 origin_type=origin_type, origin_ref=origin_ref)
        if entry_id:
            vectors.append((entry_id, c["body"]))
        return bool(entry_id)
    _check_transition(old["status"], "archived")  # 旧行归档轨迹；archived 再 UPDATE 拒绝
    new_confidence = float(c.get("confidence") or 0.0)
    if old["status"] == "degraded":
        conf = new_confidence
        if conf < revive_min_confidence:
            # 低置信证据不足以复活：只计强化，不覆盖正文、不产生轨迹行（spec §6）
            logger.warning("revive rejected: %s/%s conf=%.2f < %.2f",
                           d, t, conf, revive_min_confidence)
            conn.execute(
                "INSERT INTO review_history (entry_id,user_source,user_id,decision,actor,"
                "ts,note,entry_version) VALUES (?,?,?,'revive_rejected','pipeline',?,?,?)",
                (old["id"], us, uid, now,
                 f"confidence={conf:.2f} < {revive_min_confidence}", old["updated_at"]))
            return _noop(conn, us, uid, target, now)
        _check_transition("degraded", "active")  # 新证据演进恢复 active（spec §6）
        # F7: 复活恢复置信——降级无历史快照可考，取 max(降级残值, 新证据)，
        # 避免 0.8ⁿ 循环衰减使复活条目永远排不进装配预算
        new_confidence = max(float(old["confidence"] or 0.0), new_confidence)
    trace_seq = _superseded_identity(conn, us, uid, d, t, now)
    trace_id = new_id()
    # 轨迹行：旧行完整内容原样存档，variant='trace' + seq 与规范行共存；
    # sources 仍指向规范行 id，轨迹行只做历史快照。
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS}) "
        "SELECT user_source,user_id,dimension,topic,?,?,?,confidence,'archived',body,summary,"
        "created_at,?,last_reinforced_at,'superseded',?,topic_new,conflict_with,reinforcements,? "
        "FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
        " AND variant=?",
        (trace_id, VARIANT_TRACE, trace_seq, now, old["id"], now,
         us, uid, d, t, VARIANT_NORMAL))
    # 旧行证据链转移给轨迹行（spec §12：archived 条目保留全部字段与证据链）。
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (trace_id, us, uid, old["id"]))
    # 规范行原地覆盖新值；id/topic/variant 不变，引用无需迁移
    conn.execute(
        "UPDATE entries SET body=?,summary=?,confidence=?,status='active',degraded_reason=NULL,"
        "superseded_by=NULL,topic_new=NULL,conflict_with=NULL,updated_at=?,last_reinforced_at=? "
        "WHERE user_source=? AND user_id=? AND id=? AND variant=?",
        (c["body"], c["summary"], new_confidence, now, now, us, uid, old["id"],
         VARIANT_NORMAL))
    if old["conflict_with"]:
        # M-1：普通演进只清标记，不复活矛盾对方；仅澄清（kind=clarification）可复活。
        # 澄清路径的 pending 删除延后到管线写入成功后（I-17）。
        is_clarification = c.get("kind") == "clarification"
        resolve_conflicts(conn, us, uid, old["id"], now,
                          revive=is_clarification, clear_pending=not is_clarification)
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (old["id"], us, uid, origin_type, origin_ref, c["quote"],
         c.get("kind") or "explicit"))
    vectors.append((old["id"], c["body"]))
    return True


def _release_pending(conn, us, uid, old, now):
    """把 pending_review 行改成 snapshot 变体（仍在复核队列），腾出 normal 槽位。

    id 不变，sources/conflict_with 无需迁移；规范槽位由后续新行占用。
    """
    seq = _next_variant_seq(conn, us, uid, old["dimension"], old["topic"],
                            VARIANT_SNAPSHOT)
    conn.execute(
        "UPDATE entries SET variant=?, variant_seq=?, updated_at=?"
        " WHERE id=? AND status='pending_review'",
        (VARIANT_SNAPSHOT, seq, now, old["id"]))


def _rekey_conflict_marks(conn, us, uid, old_id, new_id):
    rows = conn.execute(
        "SELECT id, conflict_with FROM entries"
        " WHERE user_source=? AND user_id=? AND conflict_with IS NOT NULL",
        (us, uid)).fetchall()
    for row in rows:
        marks = _load_marks(row["conflict_with"])
        if not marks or old_id not in [str(m) for m in marks]:
            continue
        rewritten = [new_id if str(m) == old_id else m for m in marks]
        conn.execute(
            "UPDATE entries SET conflict_with=? WHERE id=?",
            (json.dumps(rewritten, ensure_ascii=False), row["id"]))


def _load_marks(value):
    try:
        marks = json.loads(value) if value else []
    except (TypeError, ValueError):
        return None
    return marks if isinstance(marks, list) else None


def resolve_conflicts(conn, us, uid, entry_id, now, *, revive=False, clear_pending=True):
    """矛盾收尾（spec §11 闭环）：按 id 精确清除对方行的 conflict_with 标记。

    - 仅存活行（active/degraded）参与，archived 轨迹行原样保留（M-2）；
    - 标记在 Python 侧精确解析（JSON 数组逐项比较），不做 LIKE 子串匹配（M-3）；
    - revive=True（澄清/人工判定）才把 conflict_lost 的对方恢复 active；普通演进
      UPDATE 只清标记（M-1）。
    须在调用方事务内执行。
    """
    rows = conn.execute(
        "SELECT id, status, degraded_reason, conflict_with"
        " FROM entries WHERE user_source=? AND user_id=?"
        " AND status IN ('active','degraded') AND conflict_with IS NOT NULL",
        (us, uid)).fetchall()
    for o in rows:
        marks = _load_marks(o["conflict_with"])
        if marks is None:
            continue
        keep = []
        hit = False
        for m in marks:
            # 行键（裸 id）精确匹配
            if str(m) == entry_id:
                hit = True
            else:
                keep.append(m)
        if not hit:
            continue
        new_conflict = json.dumps(keep, ensure_ascii=False) if keep else None
        if revive and o["status"] == "degraded" and o["degraded_reason"] == "conflict_lost":
            conn.execute(
                "UPDATE entries SET conflict_with=?, status='active', degraded_reason=NULL,"
                " updated_at=? WHERE id=?",
                (new_conflict, now, o["id"]))
        else:
            conn.execute(
                "UPDATE entries SET conflict_with=? WHERE id=?",
                (new_conflict, o["id"]))
    if clear_pending:
        _clear_pending_for(conn, us, uid, entry_id)


def _clear_pending_for(conn, us, uid, entry_id):
    for row in conn.execute(
            "SELECT conflicting_ids FROM pending_clarifications"
            " WHERE user_source=? AND user_id=?", (us, uid)).fetchall():
        try:
            ids = json.loads(row["conflicting_ids"])
        except (TypeError, ValueError):
            continue
        if any(str(m) == entry_id for m in ids):
            conn.execute(
                "DELETE FROM pending_clarifications"
                " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                (us, uid, row["conflicting_ids"]))


def _next_variant_seq(conn, us, uid, d, t, variant):
    """同 (用户,维度,主题,形态) 的下一个序号；MAX+1 避免删行后复用。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(variant_seq), 0) + 1 FROM entries"
        " WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND variant=?",
        (us, uid, d, t, variant)).fetchone()
    return int(row[0])


def _superseded_identity(conn, us, uid, d, t, now):
    """轨迹行序号：同 (dim,topic,trace) 取 MAX+1；时间由 archived_at 表达。"""
    return _next_variant_seq(conn, us, uid, d, t, VARIANT_TRACE)


def _conflict(conn, us, uid, c, target, now, origin_type, origin_ref, vectors) -> bool:
    if not c.get("quote"):
        return False
    if target is None:
        raise IllegalTransition("CONFLICT requires target")
    old = _fetch_row(conn, us, uid, target)
    if old is None:
        raise IllegalTransition(f"CONFLICT target not found: {target}")
    taboo = _find_taboo(conn, us, uid, c)
    # 同 key 主键唯一 → 矛盾双存行 topic 加 "+" 前缀变形落库（复核/澄清任务按标记关联）
    conflict_ids = [old["id"]]
    if taboo:
        conflict_ids.append(taboo["id"])
    is_taboo_side = c["dimension"] == "taboo"
    if is_taboo_side or taboo:
        # taboo 恒胜：taboo 侧新行不降；与 taboo 互斥的 preference 新行直接 degraded
        status, reason, confidence = ("active", None, c["confidence"]) if is_taboo_side \
            else ("degraded", "conflict_lost", c["confidence"] * 0.8)
    else:
        # 普通矛盾双存：双方 degraded(conflict_lost)、各降置信（对称性裁决）
        status, reason, confidence = "degraded", "conflict_lost", c["confidence"] * 0.8
    conflict_seq = _next_variant_seq(conn, us, uid, c["dimension"], c["topic"],
                                     VARIANT_CONFLICT)
    entry_id = _insert_entry(conn, us, uid, c,
                             status=status, now=now, origin_type=origin_type,
                             origin_ref=origin_ref, confidence=confidence,
                             degraded_reason=reason, conflict_with=conflict_ids,
                             variant=VARIANT_CONFLICT, variant_seq=conflict_seq)
    if entry_id:
        vectors.append((entry_id, c["body"]))
    new_mark = entry_id
    if old["dimension"] == "taboo":
        # 旧行是 taboo：只互标 conflict_with，不降置信不降状态
        conn.execute(
            "UPDATE entries SET conflict_with=?,updated_at=? WHERE id=?",
            (json.dumps(_merge_conflict(old["conflict_with"], new_mark), ensure_ascii=False),
             now, old["id"]))
    else:
        if old["status"] != "degraded":
            _check_transition(old["status"], "degraded")  # active→degraded；已 degraded 重复落败仅刷字段
        conn.execute(
            "UPDATE entries SET conflict_with=?,confidence=confidence*0.8,status='degraded',"
            "degraded_reason='conflict_lost',updated_at=?,last_reinforced_at=? WHERE id=?",
            (json.dumps(_merge_conflict(old["conflict_with"], new_mark), ensure_ascii=False),
             now, now, old["id"]))
    if taboo:
        _append_conflict_mark(conn, taboo, new_mark)
    return bool(entry_id)


def _noop(conn, us, uid, target, now) -> bool:
    old = _fetch_row(conn, us, uid, target) if target is not None else None
    if old is None:
        raise IllegalTransition(f"NOOP target not found: {target}")
    conn.execute(
        "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=?"
        " WHERE id=?",
        (now, old["id"]))
    return True


def _orphan(conn, us, uid, c, now, origin_type, origin_ref) -> bool:
    # 观察区键 = topic_new（词表外主题描述）：同用户同 topic_new 复用一行做 reinforce；
    # 空 topic_new 不合并（否则不同主题被压成一行，启用门槛永不满足，I-1）。
    topic_new = (c.get("topic_new") or c.get("topic") or "").strip()
    if not topic_new:
        logger.warning("drop orphan candidate without topic_new: %s", c.get("body"))
        return False
    key = ("orphan", topic_new)
    old = _fetch_row(conn, us, uid, key)
    if old is not None:
        # 同主题 orphan 复现：计数 +1（spec §8.2 启用门槛"复现≥3次/30天"依据）
        conn.execute(
            "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=?"
            " WHERE id=?",
            (now, old["id"]))
        return True
    entry_id = _insert_entry(conn, us, uid, {**c, "topic": topic_new, "topic_new": topic_new},
                             status="orphan_topic", now=now,
                             origin_type=origin_type, origin_ref=origin_ref)
    return bool(entry_id)


def _insert_entry(conn, us, uid, c, *, status, now, origin_type, origin_ref,
                  confidence=None, degraded_reason=None, conflict_with=None,
                  variant=VARIANT_NORMAL, variant_seq=0):
    quote = c.get("quote")
    if not quote:
        return None  # 硬约束（spec §5）：无 quote 的条目拒绝入库
    if confidence is None:
        confidence = c["confidence"]
    entry_id = new_id()
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS})"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (us, uid, c["dimension"], c["topic"], entry_id, variant, variant_seq,
         confidence, status, c["body"], c["summary"] or "", now, now, now,
         degraded_reason, None, c.get("topic_new"),
         json.dumps(conflict_with, ensure_ascii=False) if conflict_with else None, 0, None))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (entry_id, us, uid, origin_type, origin_ref, quote,
         c.get("kind") or "explicit"))
    return entry_id


def _merge_conflict(existing, mark_value):
    ids = json.loads(existing) if existing else []
    if mark_value not in ids:
        ids.append(mark_value)
    return ids


def _append_conflict_mark(conn, taboo_row, other_mark):
    """taboo 恒胜：taboo 行只补 conflict_with，不降置信不降状态。"""
    conn.execute(
        "UPDATE entries SET conflict_with=? WHERE id=?",
        (json.dumps(_merge_conflict(taboo_row["conflict_with"], other_mark), ensure_ascii=False),
         taboo_row["id"]))


def _has_embeddings_table(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone() is not None


def compute_vector(embedder, text: str) -> list[float] | None:
    """编辑路径的向量预计算：网络 IO 在写事务外完成，失败返回 None 由调用方降级。"""
    if embedder is None or not (text or "").strip():
        return None
    try:
        return l2_normalize(embedder.embed([text])[0])
    except _EMBED_ERRORS:
        logger.warning("vector recompute failed; edited entry will drop its vector")
        return None


def sync_entry_vector(conn, *, user_source: str, user_id: str, entry_id: str,
                      old_body: str, new_body: str, vector: list[float] | None = None,
                      vector_requested: bool = False) -> str:
    """编辑后的向量对齐：id 稳定，改键不搬向量；正文变化重嵌或删除。

    vector 由 compute_vector 在事务外预先算好；vector_requested 表示本次编辑
    本应重嵌（嵌入服务已启用），vector 为 None 即嵌入失败，按删除降级。
    返回 kept/absent/replaced/dropped/disabled，供调用方拼装提示。
    """
    if not _has_embeddings_table(conn):
        return "disabled"
    body_changed = (new_body or "").strip() != (old_body or "").strip()
    had_old = conn.execute(
        "SELECT 1 FROM embeddings WHERE entry_id=?", (entry_id,)).fetchone() is not None
    if not body_changed:
        return "kept" if had_old else "absent"
    # 正文变化：旧向量可能已不代表条目内容，先删再决定能否重建，
    # 嵌入失败或无嵌入服务时宁可不召回也不返回脏语义。
    conn.execute("DELETE FROM embeddings WHERE entry_id=?", (entry_id,))
    if not vector_requested or vector is None:
        return "dropped" if had_old else "absent"
    try:
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (entry_id, user_source, user_id, json.dumps(l2_normalize(vector))))
    except sqlite3.Error:  # 维度配置错等：条目行保留，仅跳过向量（spec §20）
        logger.exception("vector write failed for edited entry %s", entry_id)
        return "dropped"
    return "replaced"


def merge_entries(conn, us, uid, *, src_id, dst_id, now) -> dict:
    """把 src 行并入 dst 行（人工确认的 topic 归一/去重）。

    正文与摘要保留 dst（规范条目优先），置信取二者最大、强化次数累加；
    src 的证据链（sources）迁移到 dst，conflict_with 中的 src 标记重定向，
    最后删除 src 行与其向量。调用方负责事务；返回 {"merged": bool}。
    """
    src = conn.execute(
        "SELECT dimension, topic, confidence, reinforcements FROM entries"
        " WHERE user_source=? AND user_id=? AND id=?",
        (us, uid, src_id)).fetchone()
    dst = conn.execute(
        "SELECT dimension, topic FROM entries"
        " WHERE user_source=? AND user_id=? AND id=?",
        (us, uid, dst_id)).fetchone()
    if src is None or dst is None:
        return {"merged": False}
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (dst_id, us, uid, src_id))
    _rekey_conflict_marks(conn, us, uid, src_id, dst_id)
    conn.execute("UPDATE entries SET superseded_by=? WHERE superseded_by=?",
                 (dst_id, src_id))
    conn.execute(
        "UPDATE entries SET confidence=MAX(confidence, ?),"
        " reinforcements=reinforcements+?, updated_at=?"
        " WHERE user_source=? AND user_id=? AND id=?",
        (src["confidence"], src["reinforcements"], now, us, uid, dst_id))
    # src 主题的轨迹/冲突/快照跟随规范行并入 dst 主题，避免变体孤儿
    _move_topic_variants(conn, us, uid, src["dimension"], src["topic"],
                         dst["dimension"], dst["topic"], now)
    conn.execute(
        "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=?",
        (us, uid, src_id))
    if _has_embeddings_table(conn):
        conn.execute("DELETE FROM embeddings WHERE entry_id=?", (src_id,))
    return {"merged": True}


def _move_topic_variants(conn, us, uid, src_dim, src_topic, dst_dim, dst_topic, now):
    """把 src 主题下的非 normal 形态迁到 dst 主题，seq 按目标 MAX+1 重排。"""
    if (src_dim, src_topic) == (dst_dim, dst_topic):
        return
    rows = conn.execute(
        "SELECT id, variant FROM entries WHERE user_source=? AND user_id=?"
        " AND dimension=? AND topic=? AND variant<>?",
        (us, uid, src_dim, src_topic, VARIANT_NORMAL)).fetchall()
    for row in rows:
        seq = _next_variant_seq(conn, us, uid, dst_dim, dst_topic, row["variant"])
        conn.execute(
            "UPDATE entries SET dimension=?, topic=?, variant_seq=?, updated_at=?"
            " WHERE id=?",
            (dst_dim, dst_topic, seq, now, row["id"]))


def _write_vectors(conn, us, uid, vectors, precomputed):
    if not vectors or not _has_embeddings_table(conn):
        return  # embeddings 表未启用（配置降级）：整体跳过向量行
    for key, text in vectors:
        vec = precomputed.get(text)
        if vec is None:
            continue  # 预计算阶段已降级：该条目跳过向量行
        try:
            conn.execute("DELETE FROM embeddings WHERE entry_id=?", (key,))
            conn.execute(
                "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
                (key, us, uid, json.dumps(l2_normalize(vec))))
        except (json.JSONDecodeError, TypeError, sqlite3.Error):
            # 降级（spec §20）：该条目跳过向量行，条目行保留；维度配错等 sqlite 错误同样不阻断
            continue


def entry_has_vector(conn, entry_id: str) -> bool | None:
    """条目是否有向量行；embeddings.entry_id 直接存 entries.id。

    返回 True/False 仅在 embeddings 表存在时有意义：False = 表存在但该条目
    无向量行（如嵌入失败被降级跳过）。表未启用 → None，调用方据此整体跳过
    向量化告警（"未启用"不是故障，不该提示用户修复）。
    """
    if not _has_embeddings_table(conn):
        return None
    return conn.execute(
        "SELECT 1 FROM embeddings WHERE entry_id=?", (entry_id,)
    ).fetchone() is not None
