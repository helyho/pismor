"""复核判定的事务逻辑（公开 API 与管理 API 共用）。

调用方负责开启事务：本模块只做判定、版本推进与矛盾收尾，不 commit。
`entry_version` 是 `entries.updated_at` 的乐观锁标记；版本失配返回 noop。
"""
from datetime import datetime, timezone

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
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=?"
        " AND updated_at=? AND variant<>'trace'"
        " AND status IN ('pending_review','active','degraded')",
        (user_source, user_id, entry_id, entry_version)).fetchone()
    if row is None:
        return {"noop": True}

    now = utcnow()
    if decision == "fix":
        # 修正只改内容，不改变状态、不清冲突、不做快照转正，也不自动确认保留。
        cur = conn.execute(
            "UPDATE entries SET body=COALESCE(?, body), summary=COALESCE(?, summary),"
            " updated_at=?"
            " WHERE user_source=? AND user_id=? AND id=? AND updated_at=?",
            (body, summary, now, user_source, user_id, entry_id, entry_version))
        if cur.rowcount == 0:
            return {"noop": True}
        writer.sync_entry_vector(
            conn, user_source=user_source, user_id=user_id, entry_id=row["id"],
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
            "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=?"
            " AND updated_at=?",
            (user_source, user_id, entry_id, entry_version))
        if cur.rowcount == 0:
            return {"noop": True}
        conn.execute(
            "DELETE FROM sources WHERE user_source=? AND user_id=? AND entry_id=?",
            (user_source, user_id, row["id"]))
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
        ).fetchone():
            conn.execute("DELETE FROM embeddings WHERE entry_id=?", (row["id"],))
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
                " AND dimension=? AND topic=? AND variant='normal' AND id<>?"
                " AND status IN ('active','degraded','pending_review')",
                (user_source, user_id, row["dimension"], topic, row["id"])).fetchone()
            if existing is not None:
                merged = writer.merge_entries(
                    conn, user_source, user_id,
                    src_id=row["id"], dst_id=existing["id"], now=now)
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
            # 改键：id 稳定，同 topic 的全部 variant 级联到新 topic
            cur = conn.execute(
                "UPDATE entries SET status=?, topic=?, degraded_reason=NULL,"
                " conflict_with=NULL, updated_at=?"
                " WHERE user_source=? AND user_id=? AND id=? AND updated_at=?",
                (new_status, topic, now, user_source, user_id, row["id"],
                 entry_version))
            if cur.rowcount == 0:
                return {"noop": True}
            conn.execute(
                "UPDATE entries SET topic=?, updated_at=?"
                " WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND id<>?",
                (topic, now, user_source, user_id, row["dimension"],
                 row["topic"], row["id"]))
            writer.resolve_conflicts(conn, user_source, user_id, entry_id, now,
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
        snapshot = row["variant"] == "snapshot"
        fix_body = body if decision == "fix" else None
        fix_summary = summary if decision == "fix" else None
        if snapshot:
            # 待复核快照 confirm：规范槽已被新证据占用 → 快照归档（不复活旧猜测）
            occupied = conn.execute(
                "SELECT 1 FROM entries WHERE user_source=? AND user_id=?"
                " AND dimension=? AND topic=? AND variant='normal' AND id<>?",
                (user_source, user_id, row["dimension"], row["topic"],
                 row["id"])).fetchone()
            if occupied:
                trace_seq = conn.execute(
                    "SELECT COALESCE(MAX(variant_seq),0)+1 FROM entries"
                    " WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
                    " AND variant='trace'",
                    (user_source, user_id, row["dimension"], row["topic"])).fetchone()[0]
                cur = conn.execute(
                    "UPDATE entries SET variant='trace', variant_seq=?, status='archived',"
                    " archived_at=?, conflict_with=NULL, degraded_reason='superseded',"
                    " updated_at=?"
                    " WHERE user_source=? AND user_id=? AND id=? AND updated_at=?",
                    (trace_seq, now, now, user_source, user_id, row["id"], entry_version))
                if cur.rowcount == 0:
                    return {"noop": True}
                conn.execute(
                    "INSERT INTO review_history (entry_id, user_source, user_id,"
                    " decision, actor, ts, note, entry_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry_id, user_source, user_id, decision, actor, now,
                     note or "superseded_by_evolution", entry_version))
                return {"noop": False, "updated": 1}
        sets = ["status=?", "body=COALESCE(?, body)", "summary=COALESCE(?, summary)",
                "degraded_reason=NULL", "conflict_with=NULL", "updated_at=?"]
        args = [new_status, fix_body, fix_summary, now]
        if snapshot:
            # 同一条 UPDATE 同时改 variant/seq/status，满足表级 CHECK
            sets += ["variant='normal'", "variant_seq=0"]
        cur = conn.execute(
            "UPDATE entries SET {} WHERE user_source=? AND user_id=? AND id=?"
            " AND updated_at=?".format(", ".join(sets)),
            (*args, user_source, user_id, row["id"], entry_version))
        if cur.rowcount == 0:  # 并发：先到者已改版本 → 本判定作废
            return {"noop": True}
        writer.sync_entry_vector(
            conn, user_source=user_source, user_id=user_id, entry_id=row["id"],
            old_body=row["body"],
            new_body=fix_body if fix_body is not None else row["body"],
            vector=vector, vector_requested=vector_requested,
        )
        writer.resolve_conflicts(conn, user_source, user_id,
                                 entry_id, now,
                                 revive=True, clear_pending=True)
    conn.execute(
        "INSERT INTO review_history (entry_id, user_source, user_id, decision,"
        " actor, ts, note, entry_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, user_source, user_id, decision, actor, now,
         note, entry_version))
    return {"noop": False, "updated": 1}
