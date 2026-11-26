import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


_ALIVE = (
    "NOT EXISTS (SELECT 1 FROM tombstones t"
    " WHERE t.user_source=entries.user_source AND t.user_id=entries.user_id)"
)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def get_health(conn: sqlite3.Connection, db_path: Path) -> dict:
    db_path = Path(db_path)
    wal_path = db_path.with_name(db_path.name + "-wal")
    integrity = "error"
    error = None
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    except Exception as exc:  # 数据库损坏时页面仍要能说明原因
        error = f"{type(exc).__name__}: {exc}"

    journal_mode = None
    sqlite_version = None
    try:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        sqlite_version = str(conn.execute("SELECT sqlite_version()").fetchone()[0])
    except Exception:
        pass

    return {
        "db_ok": integrity == "ok",
        "integrity": integrity,
        "error": error,
        "db_bytes": _size(db_path),
        "wal_bytes": _size(wal_path),
        "journal_mode": journal_mode,
        "sqlite_version": sqlite_version,
        "entries": _scalar(conn, f"SELECT COUNT(*) FROM entries WHERE {_ALIVE}"),
        "users": _scalar(
            conn,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT user_source, user_id FROM entries WHERE {_ALIVE})",
        ),
        "pending_review": _scalar(
            conn, f"SELECT COUNT(*) FROM entries WHERE status='pending_review' AND {_ALIVE}"
        ),
        "tombstones": _scalar(conn, "SELECT COUNT(*) FROM tombstones"),
        "updated_at": conn.execute(
            f"SELECT MAX(updated_at) FROM entries WHERE {_ALIVE}"
        ).fetchone()[0],
    }


def list_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    items: list[dict] = []
    rows = conn.execute(
        "SELECT id, job, started_at, finished_at, ok, stats_json, error"
        " FROM job_runs ORDER BY started_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    for row in rows:
        stats = None
        if row["stats_json"]:
            try:
                stats = json.loads(row["stats_json"])
            except json.JSONDecodeError:
                stats = {"raw": row["stats_json"]}
        items.append({
            "id": row["id"],
            "job": row["job"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "ok": None if row["ok"] is None else bool(row["ok"]),
            "stats": stats,
            "error": row["error"],
        })
    return items


def get_stats(conn: sqlite3.Connection) -> dict:
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
    by_source = [
        {"source": row["user_source"], "count": row["count"]}
        for row in conn.execute(
            f"SELECT user_source, COUNT(*) AS count FROM entries WHERE {_ALIVE}"
            " GROUP BY user_source ORDER BY count DESC"
        )
    ]
    top_topics = [
        {
            "dimension": row["dimension"],
            "topic": row["topic"],
            "count": row["count"],
        }
        for row in conn.execute(
            f"SELECT dimension, topic, COUNT(*) AS count FROM entries"
            f" WHERE status IN ('active','degraded') AND {_ALIVE}"
            " GROUP BY dimension, topic ORDER BY count DESC, dimension LIMIT 12"
        )
    ]

    days = [
        (datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat()
        for offset in range(29, -1, -1)
    ]
    counts = {
        row["day"]: row["count"]
        for row in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS count"
            " FROM review_history WHERE substr(ts, 1, 10) >= ?"
            " AND NOT EXISTS (SELECT 1 FROM tombstones t"
            "   WHERE t.user_source=review_history.user_source"
            "     AND t.user_id=review_history.user_id)"
            " GROUP BY day",
            (days[0],),
        )
    }
    review_trend = [{"date": day, "count": counts.get(day, 0)} for day in days]

    backlog = [
        {"bucket": row["bucket"], "count": row["count"]}
        for row in conn.execute(
            "SELECT CASE"
            " WHEN julianday('now') - julianday(updated_at) < 1 THEN '<1d'"
            " WHEN julianday('now') - julianday(updated_at) < 3 THEN '1-3d'"
            " WHEN julianday('now') - julianday(updated_at) < 7 THEN '3-7d'"
            " ELSE '>7d' END AS bucket, COUNT(*) AS count"
            f" FROM entries WHERE status='pending_review' AND {_ALIVE}"
            " GROUP BY bucket ORDER BY MIN(julianday('now') - julianday(updated_at))"
        )
    ]
    return {
        "by_status": by_status,
        "by_dimension": by_dimension,
        "by_source": by_source,
        "top_topics": top_topics,
        "review_trend": review_trend,
        "pending_backlog": backlog,
    }


def list_admin_audit(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [
        {
            "id": row["id"],
            "actor": row["actor"],
            "action": row["action"],
            "target": row["target"],
            "ts": row["ts"],
            "detail": row["detail"],
        }
        for row in conn.execute(
            "SELECT id, actor, action, target, ts, detail FROM admin_audit"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        )
    ]
