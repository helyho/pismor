"""复核工作台的只读查询：队列、筛选面、条目详情与证据链。"""
import sqlite3

from pismor.core import conflict_marks
from pismor.admin.queries.common import like_any, like_pattern
from pismor.core.lifecycle import alive_sql, queue_condition


QUEUE_CONDITION = queue_condition("entries")


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
        like = like_pattern(q)
        where += f" AND ({like_any('summary', 'body', 'topic', 'user_id')})"
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


def get_entry_detail(
    conn: sqlite3.Connection,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=?"
        f" AND {alive_sql('entries')}",
        (user_source, user_id, entry_id),
    ).fetchone()
    if row is None:
        return None
    entry = dict(row)
    marks = conflict_marks.decode_or_empty(row["conflict_with"])
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
            ids = conflict_marks.decode_or_empty(item["conflicting_ids"])
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
