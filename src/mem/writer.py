"""事务写入器：Action 列表 → entries + sources + embeddings 单事务落库（spec §6/§11/§12/§20）。

形状裁决（计划裁决 5/6 + 上游裁决 a/b/c）：
- 主键 (user_source,user_id,dimension,topic,scope) 不含 status，"旧行保留+新行插入"不可行：
  UPDATE = 原行覆盖写入新值（status 恢复 active），旧行完整内容另存 archived 轨迹行，
  轨迹行 topic 加 "~" 前缀保证主键唯一（装配查 active/degraded 不受影响，审计按 status 过滤）。
- CONFLICT 同 key 双存受主键唯一约束：新行 topic 加 "+" 前缀变形落库；
  对方是 taboo 时 taboo 恒胜（spec §11）：taboo 行只补 conflict_with（§13 装配
  "taboo 出全句、对方曾观察"依赖双向标记），不降置信不降状态；preference 侧直接 degraded。
- 同批双新增 taboo+preference 同主题互不感知：处理顺序 taboo 优先，preference 行写入时
  查到同 (topic,scope) taboo 行 → 按恒胜降级。
- 引用完整性由本写入器事务保证：entries 与 sources 同事务写入。
- 向量降级：embed 抛 EmbedUnavailable/KeyError/JSONDecodeError（网关 200+异常体）
  或 embeddings 表未启用 → 该条目跳过向量行，条目行照写，不抛出（spec §20）。
"""
import json

from mem.embedder import EmbedUnavailable

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
    """非法状态迁移（spec §6 状态机之外的边），整批事务回滚。"""


def _check_transition(old_status, new_status):
    if (old_status, new_status) not in _LEGAL_TRANSITIONS:
        raise IllegalTransition(f"illegal transition: {old_status} -> {new_status}")


def apply_actions(conn, user_source, user_id, actions, embedder, origin_type, origin_ref, now):
    """单事务应用 Action 列表；非法迁移抛 IllegalTransition 整批回滚。"""
    # taboo 优先：同批双新增时 taboo 行先落库，preference 行处理时查询可见 → 恒胜降级
    ordered = sorted(actions, key=lambda a: a.get("candidate", {}).get("dimension") != "taboo")
    vectors: list[tuple[str, str]] = []  # (entry_id, 嵌入文本)
    with conn:
        for action in ordered:
            _apply_one(conn, user_source, user_id, action, origin_type, origin_ref, now, vectors)
        _write_vectors(conn, user_source, user_id, embedder, vectors)


def _apply_one(conn, us, uid, action, origin_type, origin_ref, now, vectors):
    op, c = action["op"], action["candidate"]
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


def _update(conn, us, uid, c, target, now, origin_type, origin_ref, vectors):
    if not c.get("quote"):
        return
    if target is None:
        raise IllegalTransition("UPDATE requires target")
    d, t, s = target
    old = _fetch_row(conn, us, uid, target)
    if old is None:
        raise IllegalTransition(f"UPDATE target not found: {target}")
    _check_transition(old["status"], "archived")  # 旧行归档轨迹；archived 再 UPDATE、pending/orphan 走此路均拒绝
    if old["status"] == "degraded":
        _check_transition("degraded", "active")  # 新证据演进恢复 active（spec §6）
    # 轨迹行：旧行完整内容原样存档，topic 加 "~" 前缀避开主键
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS}) "
        "SELECT user_source,user_id,dimension,?,scope,?,confidence,'archived',body,summary,"
        "created_at,?,last_reinforced_at,'superseded',?,topic_new,conflict_with,reinforcements,? "
        "FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
        (f"~{t}", f"{d}_~{t}", now, f"{d}_{t}", now, us, uid, d, t, s))
    # 原行同 key 覆盖新值（bi-temporal 旧值由轨迹行承载）
    conn.execute(
        "UPDATE entries SET body=?,summary=?,confidence=?,status='active',degraded_reason=NULL,"
        "superseded_by=NULL,topic_new=NULL,updated_at=?,last_reinforced_at=? "
        "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
        (c["body"], c["summary"], c["confidence"], now, now, us, uid, d, t, s))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"{d}_{t}", us, uid, origin_type, origin_ref, c["quote"], c.get("kind") or "explicit"))
    vectors.append((f"{d}_{t}", c["body"]))


def _conflict(conn, us, uid, c, target, now, origin_type, origin_ref, vectors):
    if not c.get("quote"):
        return
    if target is None:
        raise IllegalTransition("CONFLICT requires target")
    old = _fetch_row(conn, us, uid, target)
    if old is None:
        raise IllegalTransition(f"CONFLICT target not found: {target}")
    taboo = _find_taboo(conn, us, uid, c)
    # 同 key 主键唯一 → 矛盾双存行 topic 加 "+" 前缀变形落库（复核/澄清任务按 id 关联处理）
    conflict_ids = [old["id"]] + ([taboo["id"]] if taboo else [])
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
    vectors.append((entry_id, c["body"]))
    if old["dimension"] == "taboo":
        # 旧行是 taboo：只互标 conflict_with，不降置信不降状态
        conn.execute(
            "UPDATE entries SET conflict_with=?,updated_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
            (json.dumps(_merge_conflict(old["conflict_with"], entry_id)), now, us, uid, *target))
    else:
        if old["status"] != "degraded":
            _check_transition(old["status"], "degraded")  # active→degraded；已 degraded 重复落败仅刷字段
        conn.execute(
            "UPDATE entries SET conflict_with=?,confidence=confidence*0.8,status='degraded',"
            "degraded_reason='conflict_lost',updated_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
            (json.dumps(_merge_conflict(old["conflict_with"], entry_id)), now, us, uid, *target))
    if taboo:
        _append_conflict_mark(conn, taboo, entry_id)


def _noop(conn, us, uid, target, now):
    if target is None or _fetch_row(conn, us, uid, target) is None:
        raise IllegalTransition(f"NOOP target not found: {target}")
    conn.execute(
        "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=? "
        "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
        (now, us, uid, *target))


def _orphan(conn, us, uid, c, now, origin_type, origin_ref):
    key = ("orphan", c["topic"], c["scope"])
    if _fetch_row(conn, us, uid, key) is not None:
        # 同主题 orphan 复现：计数 +1（spec §8.2 晋升门槛"复现≥3次/30天"依据）
        conn.execute(
            "UPDATE entries SET reinforcements=reinforcements+1, last_reinforced_at=? "
            "WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
            (now, us, uid, *key))
        return
    _insert_entry(conn, us, uid, c, status="orphan_topic", now=now,
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
         json.dumps(conflict_with) if conflict_with else None, 0, None))
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (entry_id, us, uid, origin_type, origin_ref, quote, c.get("kind") or "explicit"))
    return entry_id


def _merge_conflict(existing, entry_id):
    ids = json.loads(existing) if existing else []
    if entry_id not in ids:
        ids.append(entry_id)
    return ids


def _append_conflict_mark(conn, taboo_row, other_id):
    """taboo 恒胜：taboo 行只补 conflict_with，不降置信不降状态。"""
    conn.execute(
        "UPDATE entries SET conflict_with=? WHERE user_source=? AND user_id=?"
        " AND dimension='taboo' AND topic=? AND scope=?",
        (json.dumps(_merge_conflict(taboo_row["conflict_with"], other_id)),
         taboo_row["user_source"], taboo_row["user_id"], taboo_row["topic"], taboo_row["scope"]))


def _has_embeddings_table(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone() is not None


def _write_vectors(conn, us, uid, embedder, vectors):
    if not vectors or not _has_embeddings_table(conn):
        return  # embeddings 表未启用（配置降级）：整体跳过向量行
    for entry_id, text in vectors:
        try:
            vec = embedder.embed([text])[0]
            conn.execute("DELETE FROM embeddings WHERE entry_id=?", (entry_id,))  # UPDATE 复用同 id，先清旧行
            conn.execute(
                "INSERT INTO embeddings (entry_id,user_source,user_id,embedding) VALUES (?,?,?,?)",
                (entry_id, us, uid, json.dumps(vec)))
        except (EmbedUnavailable, KeyError, json.JSONDecodeError):
            continue  # 降级（spec §20）：该条目跳过向量行，条目行保留
