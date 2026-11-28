"""条目级物理删除：预览、级联清理与 taboo 归并任务刷新。

调用方负责事务边界：本模块只执行 SQL，不 commit。删除不可恢复，级联范围
与 preview 使用同一条件（user_source + user_id + dimension + topic）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from pismor.core import conflict_marks
from pismor.serving.merge_task import taboo_count


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def preview_entry_delete(
    conn,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
    cascade: bool = True,
) -> dict | None:
    """返回删除确认所需的目标与级联清单；条目不存在 → None。"""
    row = conn.execute(
        "SELECT id, dimension, topic, variant, variant_seq, status, summary, updated_at"
        " FROM entries WHERE user_source=? AND user_id=? AND id=?",
        (user_source, user_id, entry_id),
    ).fetchone()
    if row is None:
        return None

    cascaded = []
    if cascade and row["variant"] == "normal":
        cascaded = [dict(r) for r in conn.execute(
            "SELECT id, variant, variant_seq, status, summary, updated_at"
            " FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
            " AND variant='conflict' ORDER BY variant_seq",
            (user_source, user_id, row["dimension"], row["topic"]),
        ).fetchall()]

    warnings = []
    if row["dimension"] == "taboo":
        warnings.append("该条属于 taboo（禁忌）维度，删除后该用户的必带约束立即失效。")
    elif row["dimension"] == "identity":
        warnings.append("该条属于 identity（身份）维度，删除后该用户的必带约束立即失效。")

    return {
        "target": {
            "id": row["id"],
            "dimension": row["dimension"],
            "topic": row["topic"],
            "variant": row["variant"],
            "variant_seq": row["variant_seq"],
            "status": row["status"],
            "summary": row["summary"],
            "updated_at": row["updated_at"],
        },
        "cascaded": cascaded,
        "total": 1 + len(cascaded),
        "warnings": warnings,
    }


def purge_entry(
    conn,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
    cascade_conflicts: bool = True,
    expected_version: str | None = None,
    require_statuses: tuple[str, ...] | None = None,
    forbid_variants: tuple[str, ...] = (),
    taboo_merge_threshold: int | None = None,
) -> list[str]:
    """物理删除条目及其附属数据；返回实际删除的 entry id 列表。

    - expected_version：目标行 updated_at 的 CAS；失配返回 []，不碰任何附属表。
    - require_statuses / forbid_variants：复核台 remove 的原有准入条件。
    - cascade_conflicts：仅目标为 normal 时生效，连带同维度同主题全部 conflict。
    """
    row = conn.execute(
        "SELECT id, dimension, topic, variant, status, updated_at"
        " FROM entries WHERE user_source=? AND user_id=? AND id=?",
        (user_source, user_id, entry_id),
    ).fetchone()
    if row is None:
        return []
    if expected_version is not None and row["updated_at"] != expected_version:
        return []
    if require_statuses is not None and row["status"] not in require_statuses:
        return []
    if row["variant"] in forbid_variants:
        return []

    cascaded = []
    if cascade_conflicts and row["variant"] == "normal":
        cascaded = conn.execute(
            "SELECT id, dimension, status FROM entries"
            " WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
            " AND variant='conflict'",
            (user_source, user_id, row["dimension"], row["topic"]),
        ).fetchall()

    if expected_version is not None:
        cur = conn.execute(
            "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=?"
            " AND updated_at=?",
            (user_source, user_id, entry_id, expected_version),
        )
        if cur.rowcount == 0:
            return []
    else:
        conn.execute(
            "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=?",
            (user_source, user_id, entry_id),
        )

    cascaded_ids = [r["id"] for r in cascaded]
    for cascaded_id in cascaded_ids:
        conn.execute(
            "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=?",
            (user_source, user_id, cascaded_id),
        )
    deleted_ids = [entry_id, *cascaded_ids]

    for deleted_id in deleted_ids:
        conn.execute(
            "DELETE FROM sources WHERE user_source=? AND user_id=? AND entry_id=?",
            (user_source, user_id, deleted_id),
        )
        conn.execute(
            "DELETE FROM review_history WHERE user_source=? AND user_id=? AND entry_id=?",
            (user_source, user_id, deleted_id),
        )
        conn.execute(
            "UPDATE entries SET superseded_by=NULL"
            " WHERE user_source=? AND user_id=? AND superseded_by=?",
            (user_source, user_id, deleted_id),
        )
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchone():
        for deleted_id in deleted_ids:
            conn.execute("DELETE FROM embeddings WHERE entry_id=?", (deleted_id,))

    _drop_clarifications(conn, user_source, user_id, deleted_ids)
    _clear_conflict_marks(conn, user_source, user_id, deleted_ids)

    touched_alive_taboo = any(
        r["dimension"] == "taboo" and r["status"] in ("active", "degraded")
        for r in [row, *cascaded]
    )
    if touched_alive_taboo:
        _refresh_merge_task(conn, user_source, user_id, taboo_merge_threshold)

    return deleted_ids


def _drop_clarifications(conn, user_source: str, user_id: str,
                         deleted_ids: list[str]) -> None:
    """引用任一被删 id 的待澄清整行删除：question 摘要可能已泄露，不能保留。"""
    deleted = {str(i) for i in deleted_ids}
    rows = conn.execute(
        "SELECT conflicting_ids FROM pending_clarifications"
        " WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchall()
    for row in rows:
        ids = conflict_marks.decode_or_empty(row["conflicting_ids"])
        if any(str(i) in deleted for i in ids):
            conn.execute(
                "DELETE FROM pending_clarifications"
                " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                (user_source, user_id, row["conflicting_ids"]),
            )


def _clear_conflict_marks(conn, user_source: str, user_id: str,
                          deleted_ids: list[str]) -> None:
    """清掉其他行 conflict_with 中指向被删 id 的标记；不更新 updated_at。"""
    deleted = {str(i) for i in deleted_ids}
    rows = conn.execute(
        "SELECT id, conflict_with FROM entries"
        " WHERE user_source=? AND user_id=? AND conflict_with IS NOT NULL",
        (user_source, user_id),
    ).fetchall()
    for row in rows:
        raw = row["conflict_with"]
        if conflict_marks.decode(raw) is None:
            continue
        kept = conflict_marks.remove(raw, deleted)
        if kept == raw:
            continue
        conn.execute(
            "UPDATE entries SET conflict_with=? WHERE id=?",
            (kept, row["id"]),
        )


def _refresh_merge_task(conn, user_source: str, user_id: str,
                        threshold: int | None) -> None:
    """taboo 存活条数变化后刷新 open 任务：不新建，清空旧提案。"""
    count = taboo_count(conn, user_source, user_id)
    now = _utcnow()
    conn.execute(
        "UPDATE merge_tasks SET taboo_count=?, proposal_json=NULL, updated_at=?"
        " WHERE user_source=? AND user_id=? AND status='open'",
        (count, now, user_source, user_id),
    )
    if threshold is not None and count < threshold:
        conn.execute(
            "UPDATE merge_tasks SET status='dismissed',"
            " note=COALESCE(note, 'auto_dismissed_after_delete'), updated_at=?"
            " WHERE user_source=? AND user_id=? AND status='open'",
            (now, user_source, user_id),
        )
