"""跨用户条目检索。"""
import sqlite3

from pismor.admin.queries.common import like_any, like_expr, like_pattern
from pismor.core.lifecycle import alive_sql


_ALIVE = alive_sql("entries")


def _entries_where(
    *,
    dimension: str | None = None,
    status: str | None = None,
    user_source: str | None = None,
    user_id: str | None = None,
    topic: str | None = None,
    q: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
) -> tuple[str, list]:
    clauses = [_ALIVE]
    params: list = []
    if dimension:
        clauses.append("dimension=?")
        params.append(dimension)
    if status:
        clauses.append("status=?")
        params.append(status)
    if user_source:
        clauses.append("user_source=?")
        params.append(user_source)
    if user_id:
        clauses.append("user_id=?")
        params.append(user_id)
    if topic:
        clauses.append(like_expr("topic"))
        params.append(like_pattern(topic))
    if q:
        pattern = like_pattern(q)
        clauses.append(f"({like_any('summary', 'body', 'topic', 'user_id')})")
        params.extend([pattern, pattern, pattern, pattern])
    if confidence_min is not None:
        clauses.append("confidence >= ?")
        params.append(confidence_min)
    if confidence_max is not None:
        clauses.append("confidence <= ?")
        params.append(confidence_max)
    if updated_from:
        clauses.append("updated_at >= ?")
        params.append(updated_from)
    if updated_to:
        clauses.append("updated_at <= ?")
        params.append(updated_to)
    return " AND ".join(clauses), params


def list_entries(
    conn: sqlite3.Connection,
    *,
    page: int,
    page_size: int,
    dimension: str | None = None,
    status: str | None = None,
    user_source: str | None = None,
    user_id: str | None = None,
    topic: str | None = None,
    q: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
    order: str = "updated_desc",
) -> dict:
    where, params = _entries_where(
        dimension=dimension, status=status, user_source=user_source, user_id=user_id,
        topic=topic, q=q, confidence_min=confidence_min,
        confidence_max=confidence_max, updated_from=updated_from, updated_to=updated_to,
    )
    total = conn.execute(
        f"SELECT COUNT(*) FROM entries WHERE {where}", params
    ).fetchone()[0]
    order_by = {
        "updated_desc": "updated_at DESC, rowid DESC",
        "updated_asc": "updated_at ASC, rowid ASC",
        "confidence_desc": "confidence DESC, updated_at DESC",
    }.get(order, "updated_at DESC, rowid DESC")
    rows = conn.execute(
        "SELECT id, user_source, user_id, dimension, topic, variant, variant_seq,"
        " summary, status, confidence, updated_at, last_reinforced_at, reinforcements,"
        " conflict_with,"
        " (SELECT COUNT(*) FROM sources s WHERE s.user_source=entries.user_source"
        "   AND s.user_id=entries.user_id AND s.entry_id=entries.id) AS sources_count"
        f" FROM entries WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_entry_facets(conn: sqlite3.Connection) -> dict:
    def facet(column: str) -> list[dict]:
        return [
            {"value": row["value"], "count": row["count"]}
            for row in conn.execute(
                f"SELECT {column} AS value, COUNT(*) AS count FROM entries"
                f" WHERE {_ALIVE}"
                f" GROUP BY {column} ORDER BY count DESC, value"
            )
        ]

    return {
        "dimensions": facet("dimension"),
        "statuses": facet("status"),
        "sources": facet("user_source"),
    }
