"""用户记忆删除：软删除、恢复、彻底删除；供公开 API 与管理后台共用。"""
from datetime import datetime, timezone

from pismor.core.lifecycle import is_tombstoned  # noqa: F401 (re-export)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def soft_delete_memory(conn, *, user_source: str, user_id: str,
                        requested_by: str) -> None:
    """软删除：只写 tombstone，记忆数据原样保留，可恢复。调用方负责事务边界。"""
    conn.execute(
        "INSERT INTO tombstones (user_source, user_id, deleted_at, requested_by, purged_at)"
        " VALUES (?, ?, ?, ?, NULL)"
        " ON CONFLICT(user_source, user_id) DO UPDATE SET"
        " deleted_at=excluded.deleted_at, requested_by=excluded.requested_by,"
        " purged_at=tombstones.purged_at",
        (user_source, user_id, utcnow(), requested_by))


def restore_memory(conn, *, user_source: str, user_id: str) -> str | None:
    """恢复软删除用户；返回 restored / purged / None（无删除记录）。"""
    row = conn.execute(
        "SELECT purged_at FROM tombstones WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchone()
    if row is None:
        return None
    if row["purged_at"]:
        return "purged"
    conn.execute(
        "DELETE FROM tombstones WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    )
    return "restored"


def hard_delete_memory(conn, *, user_source: str, user_id: str,
                        requested_by: str) -> None:
    """彻底删除记忆数据；保留 tombstone 并标记 purged_at，继续拦截新事件。"""
    for table in ("entries", "sources", "review_history", "pending_clarifications",
                  "merge_tasks"):
        conn.execute(f"DELETE FROM {table} WHERE user_source=? AND user_id=?",
                     (user_source, user_id))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='embeddings'").fetchone():
        conn.execute("DELETE FROM embeddings WHERE user_source=? AND user_id=?",
                     (user_source, user_id))
    now = utcnow()
    conn.execute(
        "INSERT INTO tombstones (user_source, user_id, deleted_at, requested_by, purged_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(user_source, user_id) DO UPDATE SET"
        " deleted_at=excluded.deleted_at, requested_by=excluded.requested_by,"
        " purged_at=excluded.purged_at",
        (user_source, user_id, now, requested_by, now))


