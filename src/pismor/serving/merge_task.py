"""merge_task 闭环：taboo 超载任务的落库、查询与消解（spec §13 归并复审）。

读路径（assemble）只做一次幂等 UPSERT：`uq_merge_tasks_open` 部分唯一索引保证
同一用户同时只有一条 open 任务，重复装配不重复建单。写失败一律吞掉，绝不影响画像响应。

日批（pipeline.aggregator.run_daily）用同一张表补建任务并写入 `proposal_json`
（自由 topic 清单 + 可用白名单 topic），供人工/管理端据此执行 re-key 归并。
"""
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from pismor.core.db import get_conn

logger = logging.getLogger("mem.merge_task")

_COUNT_SQL = ("SELECT COUNT(*) FROM entries WHERE user_source=? AND user_id=?"
              " AND dimension='taboo' AND status IN ('active','degraded')")
_OPEN_COLS = ("id, user_source, user_id, taboo_count, scope_filter, status,"
              " proposal_json, note, created_at, updated_at")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def taboo_count(conn, user_source: str, user_id: str,
                current_scope: str | None = None) -> int:
    """该用户 taboo 存活条数；current_scope 非空时只计 global + 当前项目（与装配同口径）。"""
    sql, args = _COUNT_SQL, [user_source, user_id]
    if current_scope:
        sql += " AND scope IN ('global', ?)"
        args.append(current_scope)
    return conn.execute(sql, args).fetchone()[0]


def upsert_open(conn, user_source: str, user_id: str, *, count: int | None = None,
                scope_filter: str | None = None,
                proposal: dict | None = None, now: str | None = None) -> None:
    """幂等建单/刷新：已有 open 任务只更新计数与提案，不重置创建时间。"""
    now = now or utcnow()
    if count is None:
        count = taboo_count(conn, user_source, user_id, scope_filter)
    proposal_json = json.dumps(proposal, ensure_ascii=False) if proposal is not None else None
    conn.execute(
        "INSERT OR IGNORE INTO merge_tasks"
        "(user_source, user_id, taboo_count, scope_filter, status, proposal_json,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
        (user_source, user_id, count, scope_filter, proposal_json, now, now),
    )
    conn.execute(
        "UPDATE merge_tasks SET taboo_count=?, scope_filter=COALESCE(?, scope_filter),"
        " proposal_json=COALESCE(?, proposal_json), updated_at=?"
        " WHERE user_source=? AND user_id=? AND status='open'",
        (count, scope_filter, proposal_json, now, user_source, user_id),
    )


class DbMergeTaskSink:
    """`assemble(audit_sink=...)` 的生产实现：独立短连接落库，失败静默。

    只在 merge_task 为真时被 `_build` 调用；构造参数随请求走，不持有外部连接，
    避免读请求与写事务共用同一连接。
    """

    def __init__(self, db_path, user_source: str, user_id: str,
                 current_scope: str | None = None):
        self.db_path = db_path
        self.user_source = user_source
        self.user_id = user_id
        self.current_scope = current_scope

    def record_merge_task(self) -> None:
        try:
            with closing(get_conn(self.db_path)) as conn, conn:
                upsert_open(conn, self.user_source, self.user_id,
                            scope_filter=self.current_scope)
        except sqlite3.Error:
            # 读路径的附加动作：任何写失败都不得影响画像响应
            logger.warning("merge_task upsert failed user=%s/%s",
                           self.user_source, self.user_id, exc_info=True)


def list_tasks(conn, *, status: str | None = "open", limit: int = 100,
               offset: int = 0) -> tuple[list[dict], int]:
    where, args = "", []
    if status:
        where, args = " WHERE status=?", [status]
    total = conn.execute("SELECT COUNT(*) FROM merge_tasks" + where, args).fetchone()[0]
    rows = conn.execute(
        "SELECT {} FROM merge_tasks{} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        .format(_OPEN_COLS, where), args + [limit, offset]).fetchall()
    return [dict(r) for r in rows], total


def resolve(conn, task_id: int, status: str, note: str | None = None) -> dict | None:
    """消解任务（done=已归并 / dismissed=不处理）；已消解任务重复调用返回 None。"""
    if status not in ("done", "dismissed"):
        raise ValueError("status must be done|dismissed")
    now = utcnow()
    cur = conn.execute(
        "UPDATE merge_tasks SET status=?, note=COALESCE(?, note), updated_at=?"
        " WHERE id=? AND status='open'",
        (status, note, now, task_id),
    )
    if cur.rowcount == 0:
        return None
    row = conn.execute("SELECT {} FROM merge_tasks WHERE id=?".format(_OPEN_COLS),
                       (task_id,)).fetchone()
    return dict(row)
