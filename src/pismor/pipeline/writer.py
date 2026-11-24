"""事务写入器：Action 列表 → entries + sources + embeddings 单事务落库（spec §6/§11/§12/§20）。

形状裁决（计划裁决 5/6 + 上游裁决 a/b/c）：
- 主键 (user_source,user_id,dimension,topic,scope) 不含 status，"旧行保留+新行插入"不可行：
  UPDATE = 原行覆盖写入新值（status 恢复 active），旧行完整内容另存 archived 轨迹行，
  轨迹行 topic 加 "~" 前缀保证主键唯一（装配查 active/degraded 不受影响，审计按 status 过滤）。
- CONFLICT 同 key 双存受主键唯一约束：新行 topic 加 "+" 前缀变形落库；
  对方是 taboo 时 taboo 恒胜（spec §11）：taboo 行只补 conflict_with（§13 装配
  "taboo 出全句、对方曾观察"依赖双向标记），不降置信不降状态；preference 侧直接 degraded。
- 跨行关联标记一律 "id@scope"（keys.mark）；旧裸 id 按所在行 scope 兼容解析。
- pending_review 不阻塞演进：同 key 新证据先把待复核行移入 "!" 快照（仍在复核队列），
  再写入规范 active 行（spec §10/§14，I-2）。
- 引用完整性由本写入器事务保证：entries 与 sources 同事务写入。
- 向量降级：embed 抛 EmbedUnavailable/KeyError/JSONDecodeError（网关 200+异常体）、
  sqlite 写入错误（维度配置错等）或 embeddings 表未启用 → 该条目跳过向量行，
  条目行照写，不抛出（spec §20）。
"""
import json
import logging
import sqlite3

from pismor.llm.embedder import EmbedUnavailable
from pismor.core.keys import mark, split_mark, valid_scope, vec_key

logger = logging.getLogger("mem.writer")

# 合法迁移边（spec §6 状态机图）；INSERT 任意初始态、NOOP 同态强化不走迁移校验
_LEGAL_TRANSITIONS = {
    ("active", "degraded"), ("active", "archived"),
    ("degraded", "active"), ("degraded", "archived"),
    ("pending_review", "active"), ("pending_review", "deleted"),
    ("orphan_topic", "active"), ("orphan_topic", "deleted"),
}

_ENTRY_COLS = ("user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,"
               "created_at,updated_at,last_reinforced_at,degraded_reason,superseded_by,"
               "topic_new,conflict_with,reinforcements,archived_at")


class IllegalTransition(Exception):
    """非法状态迁移（spec §6 状态机之外的边），单条隔离不拖垮整批。"""


def _check_transition(old_status, new_status):
    if (old_status, new_status) not in _LEGAL_TRANSITIONS:
        raise IllegalTransition(f"illegal transition: {old_status} -> {new_status}")


def apply_actions(conn, user_source, user_id, actions, embedder, origin_type, origin_ref, now):
    """单事务应用 Action 列表；scope 违规候选丢弃记日志（spec §8 作用域白名单）。"""
    # taboo 优先：同批双新增时 taboo 行先落库，preference 行处理时查询可见 → 恒胜降级
    ordered = sorted(actions, key=lambda a: a.get("candidate", {}).get("dimension") != "taboo")
    vectors: list[tuple[str, str]] = []  # (五元组合成键, 嵌入文本)
    with conn:
        for action in ordered:
            _apply_one(conn, user_source, user_id, action, origin_type, origin_ref, now, vectors)
        _write_vectors(conn, user_source, user_id, embedder, vectors)


def _apply_one(conn, us, uid, action, origin_type, origin_ref, now, vectors):
    op, c = action["op"], action["candidate"]
    if not valid_scope(c.get("scope")):
        logger.warning("drop candidate %s/%s: invalid scope %r",
                       c.get("dimension"), c.get("topic"), c.get("scope"))
        return
    if op == "ADD":
        _add(conn, us, uid, c, now, origin_type, origin_ref, vectors)
    elif op == "UPDATE":
        _update(conn, us, uid, c, action["target"], now, origin_type, origin_ref, vectors)
    elif op == "CONFLICT":
        _conflict(conn, us, uid, c, action["target"], now, origin_type, origin_ref, vectors)
    elif op == "NOOP":
        _noop(conn, us, uid, action["target"], now)
    elif op == "ORPHAN":
        _orphan(conn, us, uid, c, now, origin_type, origin_ref)
    else:
        raise ValueError(f"unknown op: {op}")


def _fetch_row(conn, us, uid, key):
    d, t, s = key
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
        (us, uid, d, t, s)).fetchone()


def _find_taboo(conn, us, uid, c):
    """同 (user, topic, scope) 的存活 taboo 条目；taboo 候选自身不构成互斥。"""
    if c["dimension"] == "taboo":
        return None
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=?"
        " AND dimension='taboo' AND topic=? AND scope=? AND status IN ('active','degraded')",
        (us, uid, c["topic"], c["scope"])).fetchone()


def _add(conn, us, uid, c, now, origin_type, origin_ref, vectors):
    if not c.get("quote"):
        return  # 硬约束（spec §5）：无 quote 的条目拒绝入库
    taboo = _find_taboo(conn, us, uid, c)
    if taboo:
        # taboo 恒胜：preference 新行直接 degraded(conflict_lost)，taboo 行只补标记
        status, reason, confidence = "degraded", "conflict_lost", c["confidence"] * 0.8
        conflict_ids = [mark(taboo["id"], taboo["scope"])]
    else:
        status, reason, confidence = "active", None, c["confidence"]
        conflict_ids = None
    entry_id = _insert_entry(conn, us, uid, c, status=status, now=now, origin_type=origin_type,
                             origin_ref=origin_ref, confidence=confidence,
                             degraded_reason=reason, conflict_with=conflict_ids)
    if entry_id:
        vectors.append((vec_key(us, uid, c["dimension"], c["topic"], c["scope"]), c["body"]))
        if taboo:
            _append_conflict_mark(conn, taboo, mark(entry_id, c["scope"]))


def _update(conn, us, uid, c, target, now, origin_type, origin_ref, vectors):
    if not c.get("quote"):
        return
    if target is None:
        raise IllegalTransition("UPDATE requires target")
    d, t, s = target
    old = _fetch_row(conn, us, uid, target)
    if old is None:
        raise IllegalTransition(f"UPDATE target not found: {target}")
    if old["status"] == "pending_review":
        # 待复核行不阻塞演进：移入 "!" 快照继续留在队列，规范键写入新 active 行
        _release_pending(conn, us, uid, old, now)
        entry_id = _insert_entry(conn, us, uid, c, status="active", now=now,
                                 origin_type=origin_type, origin_ref=origin_ref)
        if entry_id:
            vectors.append((vec_key(us, uid, d, t, s), c["body"]))
        return
    _check_transition(old["status"], "archived")  # 旧行归档轨迹；archived 再 UPDATE 拒绝
    if old["status"] == "degraded":
        _check_transition("degraded", "active")  # 新证据演进恢复 active（spec §6）
    sup_topic, sup_id = _superseded_identity(conn, us, uid, d, t, now)
    # 轨迹行：旧行完整内容原样存档，topic 加 "~" 前缀避开主键；
    # 同 key 多次 UPDATE → 时间戳+序号去重（同轮同 now 也唯一）
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS}) "
        "SELECT user_source,user_id,dimension,?,scope,?,confidence,'archived',body,summary,"
        "created_at,?,last_reinforced_at,'superseded',?,topic_new,conflict_with,reinforcements,? "
        "FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
        (sup_topic, sup_id, now, f"{d}_{t}", now, us, uid, d, t, s))
    # 旧行证据链转移给轨迹行（spec §12：archived 条目保留全部字段与证据链）。
    # sources.entry_id 存行唯一键 "{entries.id}@{scope}"（id 不含 scope，裸 id 跨 scope 撞）
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (sup_id, us, uid, f"{d}_{t}@{s}"))
    # 原行同 key 覆盖新值；旧行带冲突标记 → 本行 conflict_with 归空（spec §11 闭环）
    conn.execute(
        "UPDATE entries SET body=?,summary=?,confidence=?,status='active',degraded_reason=NULL,"
        "superseded_by=NULL,topic_new=NULL,conflict_with=NULL,updated_at=?,last_reinforced_at=? "
        "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
        (c["body"], c["summary"], c["confidence"], now, now, us, uid, d, t, s))
    if old["conflict_with"]:
        # M-1：普通演进只清标记，不复活矛盾对方；仅澄清（kind=clarification）可复活。
        # 澄清路径的 pending 删除延后到管线写入成功后（I-17）。
        is_clarification = c.get("kind") == "clarification"
        resolve_conflicts(conn, us, uid, f"{d}_{t}", s, now,
                          revive=is_clarification, clear_pending=not is_clarification)
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"{d}_{t}@{s}", us, uid, origin_type, origin_ref, c["quote"], c.get("kind") or "explicit"))
    vectors.append((vec_key(us, uid, d, t, s), c["body"]))


def _release_pending(conn, us, uid, old, now):
    """把 pending_review 行改成 "!" 快照（仍在复核队列），腾出规范键给新证据。"""
    base = f"!{old['topic']}"
    topic = base
    seq = 1
    while conn.execute(
            "SELECT 1 FROM entries WHERE user_source=? AND user_id=? AND dimension=?"
            " AND topic=? AND scope=?",
            (us, uid, old["dimension"], topic, old["scope"])).fetchone():
        seq += 1
        topic = f"{base}:{seq}"
    new_id = f"{old['dimension']}_{topic}"
    conn.execute(
        "UPDATE entries SET topic=?, id=?, updated_at=? WHERE user_source=? AND user_id=?"
        " AND dimension=? AND topic=? AND scope=?",
        (topic, new_id, now, us, uid, old["dimension"], old["topic"], old["scope"]))
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (f"{new_id}@{old['scope']}", us, uid, f"{old['id']}@{old['scope']}"))


def _load_marks(value, default_scope):
    try:
        marks = json.loads(value) if value else []
    except (TypeError, ValueError):
        return None
    return marks if isinstance(marks, list) else None


def resolve_conflicts(conn, us, uid, entry_id, scope, now, *, revive=False, clear_pending=True):
    """矛盾收尾（spec §11 闭环）：按 (id,scope) 精确清除对方行的 conflict_with 标记。

    - 仅存活行（active/degraded）参与，archived 轨迹行原样保留（M-2）；
    - 标记在 Python 侧精确解析（JSON 数组逐项比较），不做 LIKE 子串匹配（M-3）；
    - revive=True（澄清/人工判定）才把 conflict_lost 的对方恢复 active；普通演进
      UPDATE 只清标记（M-1）。
    须在调用方事务内执行。
    """
    rows = conn.execute(
        "SELECT dimension, topic, scope, status, degraded_reason, conflict_with"
        " FROM entries WHERE user_source=? AND user_id=?"
        " AND status IN ('active','degraded') AND conflict_with IS NOT NULL",
        (us, uid)).fetchall()
    for o in rows:
        marks = _load_marks(o["conflict_with"], o["scope"])
        if marks is None:
            continue
        keep = []
        hit = False
        for m in marks:
            mid, mscope = split_mark(str(m), o["scope"])
            if mid == entry_id and mscope == scope:
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
                " AND topic=? AND scope=?",
                (new_conflict, now, us, uid, o["dimension"], o["topic"], o["scope"]))
        else:
            conn.execute(
                "UPDATE entries SET conflict_with=? WHERE user_source=? AND user_id=?"
                " AND dimension=? AND topic=? AND scope=?",
                (new_conflict, us, uid, o["dimension"], o["topic"], o["scope"]))
    if clear_pending:
        _clear_pending_for(conn, us, uid, entry_id, scope)


def _clear_pending_for(conn, us, uid, entry_id, scope):
    for row in conn.execute(
            "SELECT conflicting_ids FROM pending_clarifications"
            " WHERE user_source=? AND user_id=?", (us, uid)).fetchall():
        try:
            ids = json.loads(row["conflicting_ids"])
        except (TypeError, ValueError):
            continue
        if any(split_mark(str(m), scope) == (entry_id, scope) for m in ids):
            conn.execute(
                "DELETE FROM pending_clarifications"
                " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                (us, uid, row["conflicting_ids"]))


def _superseded_identity(conn, us, uid, d, t, now):
    """轨迹行 topic/id：~{topic}:{紧凑now}:{序号}，同 key 多次 UPDATE（含同轮同 now）唯一。"""
    compact = now.replace("-", "").replace(":", "").split("+")[0]  # 2026-11-24T…+08:00 → 20261124T…
    base = f"~{t}:{compact}:"
    rows = conn.execute(
        "SELECT COUNT(*) c FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic LIKE ?",
        (us, uid, d, f"{base}%")).fetchone()
    seq = rows["c"] + 1
    topic = f"{base}{seq}"
    return topic, f"{d}_{topic}"


def _conflict(conn, us, uid, c, target, now, origin_type, origin_ref, vectors):
    if not c.get("quote"):
        return
    if target is None:
        raise IllegalTransition("CONFLICT requires target")
    old = _fetch_row(conn, us, uid, target)
    if old is None:
        raise IllegalTransition(f"CONFLICT target not found: {target}")
    taboo = _find_taboo(conn, us, uid, c)
    # 同 key 主键唯一 → 矛盾双存行 topic 加 "+" 前缀变形落库（复核/澄清任务按标记关联）
    conflict_ids = [mark(old["id"], old["scope"])]
    if taboo:
        conflict_ids.append(mark(taboo["id"], taboo["scope"]))
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
    vectors.append((vec_key(us, uid, c["dimension"], f"+{c['topic']}", c["scope"]), c["body"]))
    new_mark = mark(entry_id, c["scope"])
    if old["dimension"] == "taboo":
        # 旧行是 taboo：只互标 conflict_with，不降置信不降状态
        conn.execute(
            "UPDATE entries SET conflict_with=?,updated_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
            (json.dumps(_merge_conflict(old["conflict_with"], new_mark), ensure_ascii=False),
             now, us, uid, *target))
    else:
        if old["status"] != "degraded":
            _check_transition(old["status"], "degraded")  # active→degraded；已 degraded 重复落败仅刷字段
        conn.execute(
            "UPDATE entries SET conflict_with=?,confidence=confidence*0.8,status='degraded',"
            "degraded_reason='conflict_lost',updated_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
            (json.dumps(_merge_conflict(old["conflict_with"], new_mark), ensure_ascii=False),
             now, us, uid, *target))
    if taboo:
        _append_conflict_mark(conn, taboo, new_mark)


def _noop(conn, us, uid, target, now):
    if target is None or _fetch_row(conn, us, uid, target) is None:
        raise IllegalTransition(f"NOOP target not found: {target}")
    conn.execute(
        "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=? "
        "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
        (now, us, uid, *target))


def _orphan(conn, us, uid, c, now, origin_type, origin_ref):
    # 观察区键 = topic_new（词表外主题描述）：同用户同 topic_new 复用一行做 reinforce；
    # 空 topic_new 不合并（否则不同主题被压成一行，晋升门槛永不满足，I-1）。
    topic_new = (c.get("topic_new") or c.get("topic") or "").strip()
    if not topic_new:
        logger.warning("drop orphan candidate without topic_new: %s", c.get("body"))
        return
    key = ("orphan", topic_new, c["scope"])
    if _fetch_row(conn, us, uid, key) is not None:
        # 同主题 orphan 复现：计数 +1（spec §8.2 晋升门槛"复现≥3次/30天"依据）
        conn.execute(
            "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
            (now, us, uid, *key))
        return
    _insert_entry(conn, us, uid, {**c, "topic": topic_new, "topic_new": topic_new},
                  status="orphan_topic", now=now,
                  origin_type=origin_type, origin_ref=origin_ref)


def _insert_entry(conn, us, uid, c, *, status, now, origin_type, origin_ref,
                  confidence=None, degraded_reason=None, conflict_with=None):
    quote = c.get("quote")
    if not quote:
        return None  # 硬约束（spec §5）：无 quote 的条目拒绝入库
    if confidence is None:
        confidence = c["confidence"]
    entry_id = f"{c['dimension']}_{c['topic']}"  # id 规则 {dimension}_{topic}（spec §5）
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (us, uid, c["dimension"], c["topic"], c["scope"], entry_id,
         confidence, status, c["body"], c["summary"] or "", now, now, now,
         degraded_reason, None, c.get("topic_new"),
         json.dumps(conflict_with, ensure_ascii=False) if conflict_with else None, 0, None))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        # entry_id 存行唯一键（append scope）：id 不含 scope，跨 scope 同 topic 行的证据可归属
        (f"{entry_id}@{c['scope']}", us, uid, origin_type, origin_ref, quote,
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
        " AND dimension='taboo' AND topic=? AND scope=?",
        (json.dumps(_merge_conflict(taboo_row["conflict_with"], other_mark), ensure_ascii=False),
         taboo_row["user_source"], taboo_row["user_id"], taboo_row["topic"], taboo_row["scope"]))


def _has_embeddings_table(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone() is not None


def _write_vectors(conn, us, uid, embedder, vectors):
    if not vectors or not _has_embeddings_table(conn):
        return  # embeddings 表未启用（配置降级）：整体跳过向量行
    for key, text in vectors:
        try:
            vec = embedder.embed([text])[0]
            conn.execute("DELETE FROM embeddings WHERE entry_id=?", (key,))
            conn.execute(
                "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
                (key, us, uid, json.dumps(vec)))
        except (EmbedUnavailable, KeyError, json.JSONDecodeError, IndexError, TypeError,
                sqlite3.Error):
            # 降级（spec §20）：该条目跳过向量行，条目行保留；维度配错等 sqlite 错误同样不阻断
            continue
