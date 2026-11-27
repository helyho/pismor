"""事务写入器：Action 列表 → entries + sources + embeddings 单事务落库（spec §6/§11/§12/§20）。

形状裁决（计划裁决 5/6 + 上游裁决 a/b/c）：
- 主键 (user_source,user_id,dimension,topic) 不含 status，"旧行保留+新行插入"不可行：
  UPDATE = 原行覆盖写入新值（status 恢复 active），旧行完整内容另存 archived 轨迹行，
  轨迹行 topic 加 "~" 前缀保证主键唯一（装配查 active/degraded 不受影响，审计按 status 过滤）。
- CONFLICT 同 key 双存受主键唯一约束：新行 topic 加 "+" 前缀变形落库；
  对方是 taboo 时 taboo 恒胜（spec §11）：taboo 行只补 conflict_with（§13 装配
  "taboo 出全句、对方曾观察"依赖双向标记），不降置信不降状态；preference 侧直接 degraded。
- 行键统一为 keys.entry_key（{dim}_{topic}）：entries.id = sources.entry_id；
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

from pismor.llm.embedder import EmbedUnavailable, l2_normalize
from pismor.core.keys import entry_key, vec_key

logger = logging.getLogger("mem.writer")

# 合法迁移边（spec §6 状态机图）；INSERT 任意初始态、NOOP 同态强化不走迁移校验
_LEGAL_TRANSITIONS = {
    ("active", "degraded"), ("active", "archived"),
    ("degraded", "active"), ("degraded", "archived"),
    ("pending_review", "active"), ("pending_review", "deleted"),
    ("orphan_topic", "active"), ("orphan_topic", "deleted"),
}

_ENTRY_COLS = ("user_source,user_id,dimension,topic,id,confidence,status,body,summary,"
               "created_at,updated_at,last_reinforced_at,degraded_reason,superseded_by,"
               "topic_new,conflict_with,reinforcements,archived_at")

# 会写入向量行的 op（与 _add/_update/_conflict 的 append 点一一对应）
_VECTOR_OPS = ("ADD", "UPDATE", "CONFLICT")

# degraded → active 的复活门槛默认值（spec §6：新强化证据 conf ≥ 0.85）。
# 只约束管线自动写入；管线按 settings.gate.auto_min 传入（与显式直通门槛联动），
# 人工复核 confirm / 澄清 revive 是裁决路径，不受此限。
REVIVE_MIN_CONFIDENCE = 0.85

# 嵌入降级口径（spec §20）：单条失败只丢该条向量
_EMBED_ERRORS = (EmbedUnavailable, KeyError, json.JSONDecodeError, IndexError, TypeError)


class IllegalTransition(Exception):
    """非法状态迁移（spec §6 状态机之外的边），单条隔离不拖垮整批。"""


def _check_transition(old_status, new_status):
    if (old_status, new_status) not in _LEGAL_TRANSITIONS:
        raise IllegalTransition(f"illegal transition: {old_status} -> {new_status}")


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
    d, t = key
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
        (us, uid, d, t)).fetchone()


def _find_taboo(conn, us, uid, c):
    """同 (user, topic) 的存活 taboo 条目；taboo 候选自身不构成互斥。"""
    if c["dimension"] == "taboo":
        return None
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=?"
        " AND dimension='taboo' AND topic=? AND status IN ('active','degraded')",
        (us, uid, c["topic"])).fetchone()


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
        vectors.append((vec_key(us, uid, c["dimension"], c["topic"]), c["body"]))
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
            vectors.append((vec_key(us, uid, d, t), c["body"]))
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
                (entry_key(d, t), us, uid, now,
                 f"confidence={conf:.2f} < {revive_min_confidence}", old["updated_at"]))
            return _noop(conn, us, uid, target, now)
        _check_transition("degraded", "active")  # 新证据演进恢复 active（spec §6）
        # F7: 复活恢复置信——降级无历史快照可考，取 max(降级残值, 新证据)，
        # 避免 0.8ⁿ 循环衰减使复活条目永远排不进装配预算
        new_confidence = max(float(old["confidence"] or 0.0), new_confidence)
    sup_topic, sup_id = _superseded_identity(conn, us, uid, d, t, now)
    # 轨迹行：旧行完整内容原样存档，topic 加 "~" 前缀避开主键；
    # 同 key 多次 UPDATE → 时间戳+序号去重（同轮同 now 也唯一）
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS}) "
        "SELECT user_source,user_id,dimension,?,?,confidence,'archived',body,summary,"
        "created_at,?,last_reinforced_at,'superseded',?,topic_new,conflict_with,reinforcements,? "
        "FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
        (sup_topic, sup_id, now, entry_key(d, t), now, us, uid, d, t))
    # 旧行证据链转移给轨迹行（spec §12：archived 条目保留全部字段与证据链）。
    # sources.entry_id 存行唯一键 entries.id（即 entry_key 结果）
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (sup_id, us, uid, entry_key(d, t)))
    # 原行同 key 覆盖新值；旧行带冲突标记 → 本行 conflict_with 归空（spec §11 闭环）
    conn.execute(
        "UPDATE entries SET body=?,summary=?,confidence=?,status='active',degraded_reason=NULL,"
        "superseded_by=NULL,topic_new=NULL,conflict_with=NULL,updated_at=?,last_reinforced_at=? "
        "WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
        (c["body"], c["summary"], new_confidence, now, now, us, uid, d, t))
    if old["conflict_with"]:
        # M-1：普通演进只清标记，不复活矛盾对方；仅澄清（kind=clarification）可复活。
        # 澄清路径的 pending 删除延后到管线写入成功后（I-17）。
        is_clarification = c.get("kind") == "clarification"
        resolve_conflicts(conn, us, uid, entry_key(d, t), now,
                          revive=is_clarification, clear_pending=not is_clarification)
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (entry_key(d, t), us, uid, origin_type, origin_ref, c["quote"],
         c.get("kind") or "explicit"))
    vectors.append((vec_key(us, uid, d, t), c["body"]))
    return True


def _release_pending(conn, us, uid, old, now):
    """把 pending_review 行改成 "!" 快照（仍在复核队列），腾出规范键给新证据。"""
    base = f"!{old['topic']}"
    topic = base
    seq = 1
    while conn.execute(
            "SELECT 1 FROM entries WHERE user_source=? AND user_id=? AND dimension=?"
            " AND topic=?",
            (us, uid, old["dimension"], topic)).fetchone():
        seq += 1
        topic = f"{base}:{seq}"
    new_id = entry_key(old["dimension"], topic)
    conn.execute(
        "UPDATE entries SET topic=?, id=?, updated_at=? WHERE user_source=? AND user_id=?"
        " AND dimension=? AND topic=?",
        (topic, new_id, now, us, uid, old["dimension"], old["topic"]))
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (new_id, us, uid, old["id"]))
    # 其他行 conflict_with 里指向旧行键的标记同步改写，避免悬空标记被误判为"已裁决"
    _rekey_conflict_marks(conn, us, uid, old["id"], new_id)


def _rekey_conflict_marks(conn, us, uid, old_id, new_id):
    rows = conn.execute(
        "SELECT dimension, topic, conflict_with FROM entries"
        " WHERE user_source=? AND user_id=? AND conflict_with IS NOT NULL",
        (us, uid)).fetchall()
    for row in rows:
        marks = _load_marks(row["conflict_with"])
        if not marks or old_id not in [str(m) for m in marks]:
            continue
        rewritten = [new_id if str(m) == old_id else m for m in marks]
        conn.execute(
            "UPDATE entries SET conflict_with=? WHERE user_source=? AND user_id=?"
            " AND dimension=? AND topic=?",
            (json.dumps(rewritten, ensure_ascii=False), us, uid,
             row["dimension"], row["topic"]))


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
        "SELECT dimension, topic, status, degraded_reason, conflict_with"
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
                " updated_at=? WHERE user_source=? AND user_id=? AND dimension=?"
                " AND topic=?",
                (new_conflict, now, us, uid, o["dimension"], o["topic"]))
        else:
            conn.execute(
                "UPDATE entries SET conflict_with=? WHERE user_source=? AND user_id=?"
                " AND dimension=? AND topic=?",
                (new_conflict, us, uid, o["dimension"], o["topic"]))
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


def _superseded_identity(conn, us, uid, d, t, now):
    """轨迹行 topic/id：~{topic}:{紧凑now}:{序号}，同 key 多次 UPDATE（含同轮同 now）唯一。"""
    compact = now.replace("-", "").replace(":", "").split("+")[0]  # 2026-11-24T…+08:00 → 20261124T…
    base = f"~{t}:{compact}:"
    rows = conn.execute(
        "SELECT topic FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic LIKE ?",
        (us, uid, d, f"{base}%")).fetchall()
    # F8: COUNT 在轨迹行被人工归并物理删除后会复用已删序号（撞主键整批失败），
    # 改取现存键的最大序号 +1
    top = 0
    for r in rows:
        tail = r["topic"].rsplit(":", 1)[-1]
        if tail.isdigit():
            top = max(top, int(tail))
    seq = top + 1
    topic = f"{base}{seq}"
    return topic, entry_key(d, topic)


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
    entry_id = _insert_entry(conn, us, uid, {**c, "topic": f"+{c['topic']}"},
                             status=status, now=now, origin_type=origin_type,
                             origin_ref=origin_ref, confidence=confidence,
                             degraded_reason=reason, conflict_with=conflict_ids)
    if entry_id:
        vectors.append((vec_key(us, uid, c["dimension"], f"+{c['topic']}"), c["body"]))
    new_mark = entry_id
    if old["dimension"] == "taboo":
        # 旧行是 taboo：只互标 conflict_with，不降置信不降状态
        conn.execute(
            "UPDATE entries SET conflict_with=?,updated_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
            (json.dumps(_merge_conflict(old["conflict_with"], new_mark), ensure_ascii=False),
             now, us, uid, *target))
    else:
        if old["status"] != "degraded":
            _check_transition(old["status"], "degraded")  # active→degraded；已 degraded 重复落败仅刷字段
        conn.execute(
            "UPDATE entries SET conflict_with=?,confidence=confidence*0.8,status='degraded',"
            "degraded_reason='conflict_lost',updated_at=?,last_reinforced_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
            (json.dumps(_merge_conflict(old["conflict_with"], new_mark), ensure_ascii=False),
             now, now, us, uid, *target))
    if taboo:
        _append_conflict_mark(conn, taboo, new_mark)
    return bool(entry_id)


def _noop(conn, us, uid, target, now) -> bool:
    if target is None or _fetch_row(conn, us, uid, target) is None:
        raise IllegalTransition(f"NOOP target not found: {target}")
    conn.execute(
        "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=? "
        "WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
        (now, us, uid, *target))
    return True


def _orphan(conn, us, uid, c, now, origin_type, origin_ref) -> bool:
    # 观察区键 = topic_new（词表外主题描述）：同用户同 topic_new 复用一行做 reinforce；
    # 空 topic_new 不合并（否则不同主题被压成一行，启用门槛永不满足，I-1）。
    topic_new = (c.get("topic_new") or c.get("topic") or "").strip()
    if not topic_new:
        logger.warning("drop orphan candidate without topic_new: %s", c.get("body"))
        return False
    key = ("orphan", topic_new)
    if _fetch_row(conn, us, uid, key) is not None:
        # 同主题 orphan 复现：计数 +1（spec §8.2 启用门槛"复现≥3次/30天"依据）
        conn.execute(
            "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
            (now, us, uid, *key))
        return True
    entry_id = _insert_entry(conn, us, uid, {**c, "topic": topic_new, "topic_new": topic_new},
                             status="orphan_topic", now=now,
                             origin_type=origin_type, origin_ref=origin_ref)
    return bool(entry_id)


def _insert_entry(conn, us, uid, c, *, status, now, origin_type, origin_ref,
                  confidence=None, degraded_reason=None, conflict_with=None):
    quote = c.get("quote")
    if not quote:
        return None  # 硬约束（spec §5）：无 quote 的条目拒绝入库
    if confidence is None:
        confidence = c["confidence"]
    # 行键：entries.id 与 sources.entry_id 同值，可直接 JOIN（spec §5/§12）
    entry_id = entry_key(c["dimension"], c["topic"])
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (us, uid, c["dimension"], c["topic"], entry_id,
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
        "UPDATE entries SET conflict_with=? WHERE user_source=? AND user_id=?"
        " AND dimension='taboo' AND topic=?",
        (json.dumps(_merge_conflict(taboo_row["conflict_with"], other_mark), ensure_ascii=False),
         taboo_row["user_source"], taboo_row["user_id"], taboo_row["topic"]))


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


def sync_entry_vector(conn, *, user_source: str, user_id: str,
                      old_dimension: str, old_topic: str,
                      new_dimension: str, new_topic: str,
                      old_body: str, new_body: str, vector: list[float] | None = None,
                      vector_requested: bool = False) -> str:
    """编辑后的向量对齐：正文未变搬键/保留，正文变化重嵌或删除，绝不保留旧向量。

    vector 由 compute_vector 在事务外预先算好；vector_requested 表示本次编辑
    本应重嵌（嵌入服务已启用），vector 为 None 即嵌入失败，按删除降级。
    返回 kept/moved/absent/replaced/dropped/disabled，供调用方拼装提示。
    """
    if not _has_embeddings_table(conn):
        return "disabled"
    old_key = vec_key(user_source, user_id, old_dimension, old_topic)
    new_key = vec_key(user_source, user_id, new_dimension, new_topic)
    body_changed = (new_body or "").strip() != (old_body or "").strip()
    if not body_changed:
        if old_key == new_key:
            return "kept"
        if conn.execute("SELECT 1 FROM embeddings WHERE entry_id=?", (old_key,)).fetchone() is None:
            return "absent"  # 条目从未嵌入（如嵌入服务关闭期创建），键变了也无需搬运
        conn.execute("DELETE FROM embeddings WHERE entry_id=?", (new_key,))
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding)"
            " SELECT ?, user_source, user_id, embedding FROM embeddings WHERE entry_id=?",
            (new_key, old_key))
        conn.execute("DELETE FROM embeddings WHERE entry_id=?", (old_key,))
        return "moved"
    # 正文变化：旧向量可能已不代表条目内容，先删再决定能否重建，
    # 嵌入失败或无嵌入服务时宁可不召回也不返回脏语义。
    had_old = conn.execute(
        "SELECT 1 FROM embeddings WHERE entry_id=?", (old_key,)).fetchone() is not None
    conn.execute("DELETE FROM embeddings WHERE entry_id=?", (old_key,))
    if new_key != old_key:
        conn.execute("DELETE FROM embeddings WHERE entry_id=?", (new_key,))
    if not vector_requested or vector is None:
        return "dropped" if had_old else "absent"
    try:
        conn.execute(
            "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
            (new_key, user_source, user_id, json.dumps(l2_normalize(vector))))
    except sqlite3.Error:  # 维度配置错等：条目行保留，仅跳过向量（spec §20）
        logger.exception("vector write failed for edited entry %s", new_key)
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
    conn.execute(
        "UPDATE entries SET confidence=MAX(confidence, ?),"
        " reinforcements=reinforcements+?, updated_at=?"
        " WHERE user_source=? AND user_id=? AND id=?",
        (src["confidence"], src["reinforcements"], now, us, uid, dst_id))
    conn.execute(
        "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=?",
        (us, uid, src_id))
    if _has_embeddings_table(conn):
        conn.execute("DELETE FROM embeddings WHERE entry_id=?",
                     (vec_key(us, uid, src["dimension"], src["topic"]),))
    return {"merged": True}


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


def entry_has_vector(conn, entry_key: str) -> bool | None:
    """条目是否有向量行；表结构细节封在本写入器内。

    entry_key 是条目在 embeddings 表中的键位——writer 既有约定：该表
    entry_id 列存 vec_key 四元组组合键（非 entries.id），调用方（如
    create_entry）用 vec_key(user_source, user_id, dimension, topic) 构造。

    返回 True/False 仅在 embeddings 表存在时有意义：False = 表存在但该条目
    无向量行（如嵌入失败被降级跳过）。表未启用 → None，调用方据此整体跳过
    向量化告警（"未启用"不是故障，不该提示用户修复）。
    """
    if not _has_embeddings_table(conn):
        return None
    return conn.execute(
        "SELECT 1 FROM embeddings WHERE entry_id=?", (entry_key,)
    ).fetchone() is not None
