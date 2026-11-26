from pathlib import Path
import sqlite3


_ALIVE = (
    "NOT EXISTS (SELECT 1 FROM tombstones t"
    " WHERE t.user_source=entries.user_source AND t.user_id=entries.user_id)"
)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


def get_overview(conn: sqlite3.Connection, db_path: Path) -> dict:
    db_path = Path(db_path)
    wal_path = db_path.with_name(db_path.name + "-wal")
    by_status = [
        {"status": row["status"], "count": row["count"]}
        for row in conn.execute(
            f"SELECT status, COUNT(*) AS count FROM entries WHERE {_ALIVE}"
            " GROUP BY status ORDER BY count DESC"
        )
    ]
    by_dimension = [
        {"dimension": row["dimension"], "count": row["count"]}
        for row in conn.execute(
            f"SELECT dimension, COUNT(*) AS count FROM entries WHERE {_ALIVE}"
            " GROUP BY dimension ORDER BY dimension"
        )
    ]
    return {
        "users": _scalar(
            conn,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT user_source, user_id FROM entries WHERE {_ALIVE})",
        ),
        "entries": _scalar(conn, "SELECT COUNT(*) FROM entries"),
        "pending_review": _scalar(
            conn, f"SELECT COUNT(*) FROM entries WHERE status='pending_review' AND {_ALIVE}"
        ),
        "conflicts": _scalar(
            conn,
            f"SELECT COUNT(*) FROM entries WHERE conflict_with IS NOT NULL"
            f" AND status IN ('active','degraded') AND {_ALIVE}",
        ),
        "tombstones": _scalar(conn, "SELECT COUNT(*) FROM tombstones"),
        "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "by_status": by_status,
        "by_dimension": by_dimension,
        "updated_at": conn.execute(
            f"SELECT MAX(updated_at) FROM entries WHERE {_ALIVE}"
        ).fetchone()[0],
    }
