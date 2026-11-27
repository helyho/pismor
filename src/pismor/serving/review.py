"""复核判定的事务逻辑（公开 API 与管理 API 共用）。

调用方负责开启事务：本模块只做判定、版本推进与矛盾收尾，不 commit。
`entry_version` 是 `entries.updated_at` 的乐观锁标记；版本失配返回 noop。
"""
from datetime import datetime, timezone

from pismor.core.keys import entry_key, vec_key
from pismor.pipeline import writer


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _pending_alias(row, topic, taxonomy) -> dict | None:
    """归一成功时给调用方的别名补写意图；只描述不落盘，由调用方在事务外处理。"""
    if taxonomy is None or not topic:
        return None
    return {"dim": row["dimension"], "alias": row["topic"], "canonical": topic}


def apply_review_decision(
    conn,
    *,
    user_source: str,
    user_id: str,
    scope: str,
    entry_id: str,
    decision: str,
    entry_version: str,
    note: str | None = None,
    body: str | None = None,
    summary: str | None = None,
    actor: str = "reviewer",
    topic: str | None = None,
    taxonomy=None,
    vector: list[float] | None = None,
    vector_requested: bool = False,
) -> dict:
    """执行 confirm / remove / fix；先到者生效，后到者版本失配返回 noop。

    topic: confirm 时把条目归一到该维度白名单内的 topic（taboo 碎片治理）。
      - 目标 topic 已有存活行 → 合并进目标行（证据迁移、源行删除），返回 merged_into；
      - 目标不存在 → re-key 到新 topic，返回 rekeyed_to；
      - 不在白名单 → {"noop": False, "invalid_topic": True}，不改任何数据。
    """
    row = conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=? AND scope=?"
        " AND updated_at=? AND topic NOT LIKE '~%'"
        " AND status IN ('pending_review','active','degraded')",
        (user_source, user_id, entry_id, scope, entry_version)).fetchone()
    if row is None:
        return {"noop": True}

    now = utcnow()
    if decision == "fix":
        # 修正只改内容，不改变状态、不清冲突、不做快照转正，也不自动确认保留。
        cur = conn.execute(
            "UPDATE entries SET body=COALESCE(?, body), summary=COALESCE(?, summary),"
            " updated_at=?"
            " WHERE user_source=? AND user_id=? AND id=? AND scope=? AND updated_at=?",
            (body, summary, now, user_source, user_id, entry_id, scope, entry_version))
        if cur.rowcount == 0:
            return {"noop": True}
        writer.sync_entry_vector(
            conn,
            user_source=user_source, user_id=user_id,
            old_dimension=row["dimension"], old_topic=row["topic"], old_scope=row["scope"],
            new_dimension=row["dimension"], new_topic=row["topic"], new_scope=row["scope"],
            old_body=row["body"], new_body=body if body is not None else row["body"],
            vector=vector, vector_requested=vector_requested,
        )
        conn.execute(
            "INSERT INTO review_history (entry_id, user_source, user_id, decision,"
            " actor, ts, note, entry_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, user_source, user_id, decision, actor, now, note, entry_version))
        return {"noop": False, "updated": 1}

    if decision == "remove":
        # CAS：删除同样以 updated_at 为条件，行数 0 即判定作废
        cur = conn.execute(
            "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=? AND scope=?"
            " AND updated_at=?",
            (user_source, user_id, entry_id, scope, entry_version))
        if cur.rowcount == 0:
            return {"noop": True}
        conn.execute(
            "DELETE FROM sources WHERE user_source=? AND user_id=? AND entry_id=?",
            (user_source, user_id, row["id"]))
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
        ).fetchone():
            conn.execute("DELETE FROM embeddings WHERE entry_id=?", (vec_key(
                user_source, user_id, row["dimension"], row["topic"], row["scope"]),))
    else:
        # orphan 候选 confirm → 观察区（spec §8.2 启用链入口），其余 → active
        new_status = "orphan_topic" if row["dimension"] == "orphan" else "active"
        # 人工归一：把自由 topic 收敛到白名单（taboo 禁止运行期扩充的唯一入口）
        if topic is not None and topic != row["topic"]:
            dim = taxonomy.dimensions.get(row["dimension"]) if taxonomy else None
            allowed = set(getattr(dim, "topics", None) or []) if dim else set()
            if topic not in allowed:
                return {"noop": False, "invalid_topic": True}
            existing = conn.execute(
                "SELECT id FROM entries WHERE user_source=? AND user_id=?"
                " AND dimension=? AND topic=? AND scope=? AND id<>?"
                " AND topic NOT LIKE '~%' AND topic NOT LIKE '!%'"
                " AND status IN ('active','degraded','pending_review')",
                (user_source, user_id, row["dimension"], topic, scope, row["id"])).fetchone()
            if existing is not None:
                merged = writer.merge_entries(
                    conn, user_source, user_id,
                    src_id=row["id"], src_scope=scope,
                    dst_id=existing["id"], dst_scope=scope, now=now)
                if not merged["merged"]:
                    return {"noop": True}
                conn.execute(
                    "INSERT INTO review_history (entry_id, user_source, user_id, decision,"
                    " actor, ts, note, entry_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry_id, user_source, user_id, "merge", actor, now,
                     note or f"merged_into:{topic}", entry_version))
                result = {"noop": False, "updated": 1, "merged_into": topic}
                pending = _pending_alias(row, topic, taxonomy)
                if pending is not None:
                    result["_pending_alias"] = pending
                return result
            new_id = entry_key(row["dimension"], topic, scope)
            cur = conn.execute(
                "UPDATE entries SET status=?, topic=?, id=?, degraded_reason=NULL,"
                " conflict_with=NULL, updated_at=?"
                " WHERE user_source=? AND user_id=? AND dimension=?"
                " AND topic=? AND scope=? AND updated_at=?",
                (new_status, topic, new_id, now, user_source, user_id,
                 row["dimension"], row["topic"], scope, entry_version))
            if cur.rowcount == 0:
                return {"noop": True}
            conn.execute(
                "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=?"
                " AND entry_id=?",
                (new_id, user_source, user_id, row["id"]))
            writer.sync_entry_vector(
                conn,
                user_source=user_source, user_id=user_id,
                old_dimension=row["dimension"], old_topic=row["topic"], old_scope=scope,
                new_dimension=row["dimension"], new_topic=topic, new_scope=scope,
                old_body=row["body"], new_body=row["body"],
            )
            writer.resolve_conflicts(conn, user_source, user_id, entry_id, scope, now,
                                     revive=True, clear_pending=True)
            conn.execute(
                "INSERT INTO review_history (entry_id, user_source, user_id, decision,"
                " actor, ts, note, entry_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_id, user_source, user_id, "rekey", actor, now,
                 note or f"rekeyed_to:{topic}", entry_version))
            result = {"noop": False, "updated": 1, "rekeyed_to": topic}
            pending = _pending_alias(row, topic, taxonomy)
            if pending is not None:
                result["_pending_alias"] = pending
            return result
        new_topic, new_id = row["topic"], row["id"]
        fix_body = body if decision == "fix" else None
        fix_summary = summary if decision == "fix" else None
        if row["topic"].startswith("!"):
            # 待复核快照 confirm：规范键已被新证据占用 → 快照归档（不复活旧猜测）
            canonical = row["topic"][1:]
            occupied = conn.execute(
                "SELECT 1 FROM entries WHERE user_source=? AND user_id=?"
                " AND dimension=? AND topic=? AND scope=? AND id<>?",
                (user_source, user_id, row["dimension"], canonical, scope,
                 row["id"])).fetchone()
            if occupied:
                cur = conn.execute(
                    "UPDATE entries SET status='archived', archived_at=?,"
                    " conflict_with=NULL, degraded_reason=NULL, updated_at=?"
                    " WHERE user_source=? AND user_id=? AND dimension=?"
                    " AND topic=? AND scope=? AND updated_at=?",
                    (now, now, user_source, user_id, row["dimension"],
                     row["topic"], scope, entry_version))
                if cur.rowcount == 0:
                    return {"noop": True}
                conn.execute(
                    "INSERT INTO review_history (entry_id, user_source, user_id,"
                    " decision, actor, ts, note, entry_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry_id, user_source, user_id, decision, actor, now,
                     note or "superseded_by_evolution", entry_version))
                return {"noop": False, "updated": 1}
            new_topic, new_id = canonical, entry_key(
                row["dimension"], canonical, scope)
        cur = conn.execute(
            "UPDATE entries SET status=?, topic=?, id=?,"
            " body=COALESCE(?, body), summary=COALESCE(?, summary),"
            " degraded_reason=NULL, conflict_with=NULL, updated_at=?"
            " WHERE user_source=? AND user_id=? AND dimension=?"
            " AND topic=? AND scope=? AND updated_at=?",
            (new_status, new_topic, new_id, fix_body, fix_summary, now,
             user_source, user_id, row["dimension"], row["topic"], scope,
             entry_version))
        if cur.rowcount == 0:  # 并发：先到者已改版本 → 本判定作废
            return {"noop": True}
        if new_id != row["id"]:  # 快照转正：证据链键跟随
            conn.execute(
                "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=?"
                " AND entry_id=?",
                (new_id, user_source, user_id, row["id"]))
        writer.sync_entry_vector(
            conn,
            user_source=user_source, user_id=user_id,
            old_dimension=row["dimension"], old_topic=row["topic"], old_scope=scope,
            new_dimension=row["dimension"], new_topic=new_topic, new_scope=scope,
            old_body=row["body"],
            new_body=fix_body if fix_body is not None else row["body"],
            vector=vector, vector_requested=vector_requested,
        )
        # 矛盾收尾（C-1）：按 (id,scope) 清对方行标记、conflict_lost 恢复 active
        writer.resolve_conflicts(conn, user_source, user_id,
                                 entry_id, scope, now,
                                 revive=True, clear_pending=True)
    conn.execute(
        "INSERT INTO review_history (entry_id, user_source, user_id, decision,"
        " actor, ts, note, entry_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, user_source, user_id, decision, actor, now,
         note, entry_version))
    return {"noop": False, "updated": 1}
