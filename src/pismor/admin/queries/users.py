"""用户列表、删除记录与用户记忆详情查询。"""
import json
import sqlite3

from pismor.admin.queries.common import like_pattern


_DIMENSION_ORDER = (
    "CASE dimension WHEN 'taboo' THEN 0 WHEN 'identity' THEN 1"
    " WHEN 'preference' THEN 2 WHEN 'fact' THEN 3 WHEN 'context' THEN 4 ELSE 5 END"
)


def _users_where(*, q: str | None = None, user_source: str | None = None) -> tuple[str, list]:
    clauses = [
        "NOT EXISTS (SELECT 1 FROM tombstones t"
        " WHERE t.user_source=entries.user_source AND t.user_id=entries.user_id)"
    ]
    params: list[str] = []
    if user_source:
        clauses.append("user_source=?")
        params.append(user_source)
    if q:
        pattern = like_pattern(q)
        clauses.append(
            "(user_id LIKE ? ESCAPE '\\' OR user_source LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern])
    return " AND ".join(clauses), params


def list_users(
    conn: sqlite3.Connection,
    *,
    page: int,
    page_size: int,
    q: str | None = None,
    user_source: str | None = None,
    order: str = "recent",
) -> dict:
    where, params = _users_where(q=q, user_source=user_source)
    total = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM entries WHERE {where}"
        " GROUP BY user_source, user_id)",
        params,
    ).fetchone()[0]
    sort = "ASC" if order == "oldest" else "DESC"
    if order == "entries":
        order_by = "entries DESC, updated_at DESC"
    else:
        order_by = f"updated_at {sort}, user_source, user_id"
    rows = conn.execute(
        "SELECT user_source, user_id, COUNT(*) AS entries,"
        " SUM(CASE WHEN status='pending_review' THEN 1 ELSE 0 END) AS pending_review,"
        " SUM(CASE WHEN conflict_with IS NOT NULL AND status IN ('active','degraded')"
        "          THEN 1 ELSE 0 END) AS conflicts,"
        " MAX(updated_at) AS updated_at"
        f" FROM entries WHERE {where}"
        f" GROUP BY user_source, user_id ORDER BY {order_by} LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def list_tombstones(
    conn: sqlite3.Connection,
    *,
    page: int,
    page_size: int,
    q: str | None = None,
) -> dict:
    clauses = ["1=1"]
    params: list[str] = []
    if q:
        pattern = like_pattern(q)
        clauses.append(
            "(user_id LIKE ? ESCAPE '\\' OR user_source LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern])
    where = " AND ".join(clauses)
    total = conn.execute(
        f"SELECT COUNT(*) FROM tombstones WHERE {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT user_source, user_id, deleted_at, requested_by, purged_at FROM tombstones"
        f" WHERE {where} ORDER BY deleted_at DESC, user_source, user_id"
        " LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_user_detail(
    conn: sqlite3.Connection,
    *,
    user_source: str,
    user_id: str,
) -> dict | None:
    summary = conn.execute(
        "SELECT COUNT(*) AS entries,"
        " SUM(CASE WHEN status='pending_review' THEN 1 ELSE 0 END) AS pending_review,"
        " SUM(CASE WHEN conflict_with IS NOT NULL AND status IN ('active','degraded')"
        "          THEN 1 ELSE 0 END) AS conflicts,"
        " MIN(created_at) AS first_seen, MAX(updated_at) AS updated_at,"
        " MAX(last_reinforced_at) AS last_reinforced_at"
        " FROM entries WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchone()
    tombstone = conn.execute(
        "SELECT deleted_at, requested_by, purged_at FROM tombstones"
        " WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchone()
    entries_count = int(summary["entries"] or 0)
    if entries_count == 0 and tombstone is None:
        return None

    entries = [
        dict(row)
        for row in conn.execute(
            "SELECT e.id, e.dimension, e.topic, e.scope, e.status, e.confidence,"
            " e.summary, e.body, e.created_at, e.updated_at, e.last_reinforced_at,"
            " e.degraded_reason, e.superseded_by, e.topic_new, e.conflict_with,"
            " e.reinforcements, e.archived_at,"
            " (SELECT COUNT(*) FROM sources s WHERE s.user_source=e.user_source"
            "   AND s.user_id=e.user_id AND s.entry_id=e.id) AS sources_count"
            " FROM entries e WHERE e.user_source=? AND e.user_id=?"
            f" ORDER BY {_DIMENSION_ORDER}, e.topic, e.scope",
            (user_source, user_id),
        )
    ]
    by_dimension = [
        {"dimension": row["dimension"], "count": row["count"]}
        for row in conn.execute(
            "SELECT dimension, COUNT(*) AS count FROM entries"
            " WHERE user_source=? AND user_id=? GROUP BY dimension"
            f" ORDER BY {_DIMENSION_ORDER}",
            (user_source, user_id),
        )
    ]
    by_status = [
        {"status": row["status"], "count": row["count"]}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM entries"
            " WHERE user_source=? AND user_id=? GROUP BY status ORDER BY count DESC",
            (user_source, user_id),
        )
    ]
    clarifications = []
    for item in conn.execute(
        "SELECT conflicting_ids, question, issued_at, ttl_days FROM pending_clarifications"
        " WHERE user_source=? AND user_id=? ORDER BY issued_at DESC",
        (user_source, user_id),
    ):
        try:
            ids = json.loads(item["conflicting_ids"])
        except (TypeError, ValueError):
            ids = []
        clarifications.append({
            "conflicting_ids": [str(v) for v in ids] if isinstance(ids, list) else [],
            "question": item["question"],
            "issued_at": item["issued_at"],
            "ttl_days": item["ttl_days"],
        })
    return {
        "user_source": user_source,
        "user_id": user_id,
        "entries": entries,
        "by_dimension": by_dimension,
        "by_status": by_status,
        "clarifications": clarifications,
        "tombstone": dict(tombstone) if tombstone is not None else None,
        "entries_count": entries_count,
        "pending_review": int(summary["pending_review"] or 0),
        "conflicts": int(summary["conflicts"] or 0),
        "first_seen": summary["first_seen"],
        "updated_at": summary["updated_at"],
        "last_reinforced_at": summary["last_reinforced_at"],
    }
