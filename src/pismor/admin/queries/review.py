"""复核工作台的只读查询：队列、筛选面、条目详情与证据链。"""
import json
import sqlite3


QUEUE_CONDITION = (
    "NOT EXISTS (SELECT 1 FROM tombstones t"
    " WHERE t.user_source=entries.user_source AND t.user_id=entries.user_id)"
    " AND (status='pending_review' OR (variant='conflict' AND status IN ('active','degraded')"
    " AND conflict_with IS NOT NULL))"
)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def queue_where(
    *,
    dimension: str | None = None,
    user_source: str | None = None,
    user_id: str | None = None,
    q: str | None = None,
) -> tuple[str, tuple]:
    where = QUEUE_CONDITION
    params: list[str] = []
    if dimension:
        where += " AND dimension=?"
        params.append(dimension)
    if user_source:
        where += " AND user_source=?"
        params.append(user_source)
    if user_id:
        where += " AND user_id=?"
        params.append(user_id)
    if q:
        like = f"%{_escape_like(q)}%"
        where += (
            " AND (summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\'"
            " OR topic LIKE ? ESCAPE '\\' OR user_id LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like, like, like])
    return where, tuple(params)


def list_queue(
    conn: sqlite3.Connection,
    *,
    page: int,
    page_size: int,
    dimension: str | None = None,
    user_source: str | None = None,
    user_id: str | None = None,
    q: str | None = None,
    order: str = "oldest",
) -> dict:
    where, params = queue_where(
        dimension=dimension, user_source=user_source, user_id=user_id, q=q
    )
    total = conn.execute(
        f"SELECT COUNT(*) FROM entries WHERE {where}", params
    ).fetchone()[0]
    sort = "DESC" if order == "newest" else "ASC"
    rows = conn.execute(
        "SELECT id AS entry_id, user_source, user_id, dimension, topic, summary,"
        f" confidence, status, variant, updated_at AS entry_version, degraded_reason AS note"
        f" FROM entries WHERE {where}"
        f" ORDER BY updated_at {sort}, rowid {sort} LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_facets(conn: sqlite3.Connection) -> dict:
    where, params = queue_where()
    dimensions = [
        {"value": row["value"], "count": row["count"]}
        for row in conn.execute(
            f"SELECT dimension AS value, COUNT(*) AS count FROM entries WHERE {where}"
            " GROUP BY dimension ORDER BY dimension",
            params,
        )
    ]
    sources = [
        {"value": row["value"], "count": row["count"]}
        for row in conn.execute(
            f"SELECT user_source AS value, COUNT(*) AS count FROM entries WHERE {where}"
            " GROUP BY user_source ORDER BY user_source",
            params,
        )
    ]
    return {"dimensions": dimensions, "sources": sources}


def _parse_marks(value: str | None) -> list[str]:
    try:
        marks = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return [str(mark) for mark in marks] if isinstance(marks, list) else []


def get_entry_detail(
    conn: sqlite3.Connection,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=?"
        " AND NOT EXISTS (SELECT 1 FROM tombstones t"
        "   WHERE t.user_source=entries.user_source AND t.user_id=entries.user_id)",
        (user_source, user_id, entry_id),
    ).fetchone()
    if row is None:
        return None
    entry = dict(row)
    marks = _parse_marks(row["conflict_with"])
    entry["conflict_marks"] = marks

    sources = [
        dict(source)
        for source in conn.execute(
            "SELECT seq, origin_type, origin_ref, quote, kind FROM sources"
            " WHERE user_source=? AND user_id=? AND entry_id=? ORDER BY seq",
            (user_source, user_id, entry_id),
        )
    ]
    history = [
        dict(item)
        for item in conn.execute(
            "SELECT decision, actor, ts, note, entry_version FROM review_history"
            " WHERE entry_id=? AND user_source=? AND user_id=?"
            " ORDER BY ts DESC, rowid DESC",
            (entry_id, user_source, user_id),
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
    conflicts = []
    for mark in marks:
        if mark == row["id"]:
            continue
        other = conn.execute(
            "SELECT id, dimension, topic, summary, status FROM entries"
            " WHERE user_source=? AND user_id=? AND id=?",
            (user_source, user_id, mark),
        ).fetchone()
        if other is not None:
            conflicts.append(dict(other))
    return {
        "entry": entry,
        "sources": sources,
        "history": history,
        "clarifications": clarifications,
        "conflicts": conflicts,
    }
