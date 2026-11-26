"""管理后台条目编辑：提交前校验、键变更与关联数据同步。

调用方负责开启事务；本模块不 commit。条目五元组
(user_source,user_id,dimension,topic,scope) 是主键，id 是派生的 mark，
编辑键时必须同步 sources / review_history / conflict_with / 澄清与向量。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from pismor.core.keys import entry_key, valid_scope, vec_key


STATUSES = frozenset({
    "pending_review", "orphan_topic", "active", "degraded", "archived",
})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _entry_row(conn, *, user_source: str, user_id: str, entry_id: str, scope: str):
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=? AND scope=?",
        (user_source, user_id, entry_id, scope),
    ).fetchone()


def check_entry_edit(
    conn,
    taxonomy,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
    scope: str,
    new_dimension: str,
    new_topic: str,
    new_scope: str,
    confidence: float,
    status: str,
    summary: str,
    body: str,
    degraded_reason: str | None = None,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    conflicts: list[dict] = []

    row = _entry_row(
        conn, user_source=user_source, user_id=user_id,
        entry_id=entry_id, scope=scope,
    )
    if row is None:
        return {
            "ok": False,
            "errors": ["条目不存在或已被删除"],
            "warnings": [],
            "conflicts": [],
        }
    if conn.execute(
        "SELECT 1 FROM tombstones WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchone():
        return {
            "ok": False,
            "errors": ["用户已删除，恢复后才能编辑条目"],
            "warnings": [],
            "conflicts": [],
        }

    new_dimension = (new_dimension or "").strip()
    new_topic = (new_topic or "").strip()
    new_scope = (new_scope or "").strip()
    summary = (summary or "").strip()
    body = (body or "").strip()

    if not valid_scope(new_scope):
        errors.append("作用域必须是 global 或 project:<name>")
    if status not in STATUSES:
        errors.append(f"未知状态: {status}")
    if not 0 <= confidence <= 1:
        errors.append("置信度必须在 0 到 1 之间")
    if not summary:
        errors.append("摘要不能为空")
    if not body:
        errors.append("正文不能为空")

    dimension = taxonomy.dimensions.get(new_dimension)
    if dimension is None:
        errors.append(f"维度不存在: {new_dimension}")
    else:
        if new_topic not in dimension.topics:
            warnings.append("主题不在当前词表中；本次只修改存量条目，不会新增主题")
        if new_topic in dimension.disabled_topics:
            warnings.append("主题已停用；停用只阻止新条目，存量条目仍可编辑")
    if status == "degraded" and not (degraded_reason or "").strip():
        warnings.append("状态为降级但未填写降级原因")

    if not errors:
        rows = conn.execute(
            "SELECT id, dimension, topic, scope, status, summary FROM entries"
            " WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?"
            " AND NOT (id=? AND scope=?)",
            (
                user_source, user_id, new_dimension, new_topic, new_scope,
                entry_id, scope,
            ),
        ).fetchall()
        conflicts = [dict(item) for item in rows]

    return {
        "ok": not errors and not conflicts,
        "errors": errors,
        "warnings": warnings,
        "conflicts": conflicts,
    }


def _has_embeddings_table(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchone() is not None


def _replace_marks(value: str | None, old_id: str, old_scope: str, new_id: str) -> str | None:
    if not value:
        return value
    try:
        items = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
    if not isinstance(items, list):
        return value
    old_mark = f"{old_id}@{old_scope}"
    changed = False
    out: list = []
    for item in items:
        text = str(item)
        if text in (old_id, old_mark):
            out.append(new_id)
            changed = True
        else:
            out.append(item)
    return json.dumps(sorted(set(out)), ensure_ascii=False) if changed else value


def _rename_references(
    conn,
    *,
    user_source: str,
    user_id: str,
    old_id: str,
    old_scope: str,
    new_id: str,
) -> None:
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (new_id, user_source, user_id, old_id),
    )
    conn.execute(
        "UPDATE review_history SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (new_id, user_source, user_id, old_id),
    )
    old_mark = f"{old_id}@{old_scope}"
    conn.execute(
        "UPDATE entries SET superseded_by=? WHERE user_source=? AND user_id=?"
        " AND (superseded_by=? OR superseded_by=?)",
        (new_id, user_source, user_id, old_mark, old_id),
    )

    for item in conn.execute(
        "SELECT id, scope, conflict_with FROM entries"
        " WHERE user_source=? AND user_id=? AND conflict_with IS NOT NULL",
        (user_source, user_id),
    ).fetchall():
        replaced = _replace_marks(item["conflict_with"], old_id, old_scope, new_id)
        if replaced != item["conflict_with"]:
            conn.execute(
                "UPDATE entries SET conflict_with=? WHERE user_source=? AND user_id=?"
                " AND id=? AND scope=?",
                (replaced, user_source, user_id, item["id"], item["scope"]),
            )

    for item in conn.execute(
        "SELECT user_source, user_id, conflicting_ids FROM pending_clarifications"
        " WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchall():
        replaced = _replace_marks(item["conflicting_ids"], old_id, old_scope, new_id)
        if replaced == item["conflicting_ids"]:
            continue
        try:
            conn.execute(
                "UPDATE pending_clarifications SET conflicting_ids=?"
                " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                (replaced, item["user_source"], item["user_id"], item["conflicting_ids"]),
            )
        except sqlite3.IntegrityError:
            # 新标记组合已有待确认问题：旧行已被覆盖，删除重复行即可。
            conn.execute(
                "DELETE FROM pending_clarifications"
                " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                (item["user_source"], item["user_id"], item["conflicting_ids"]),
            )


def _sync_embedding(
    conn,
    *,
    user_source: str,
    user_id: str,
    old_dimension: str,
    old_topic: str,
    old_scope: str,
    new_dimension: str,
    new_topic: str,
    new_scope: str,
    body_changed: bool,
) -> None:
    if not _has_embeddings_table(conn):
        return
    old_key = vec_key(user_source, user_id, old_dimension, old_topic, old_scope)
    new_key = vec_key(user_source, user_id, new_dimension, new_topic, new_scope)
    if body_changed:
        # 正文变化后旧向量不再代表条目内容；没有同步重建就删掉，避免脏召回。
        conn.execute("DELETE FROM embeddings WHERE entry_id=?", (old_key,))
        return
    if old_key == new_key:
        return
    conn.execute("DELETE FROM embeddings WHERE entry_id=?", (new_key,))
    conn.execute(
        "INSERT INTO embeddings (entry_id,user_source,user_id,embedding)"
        " SELECT ?, user_source, user_id, embedding FROM embeddings WHERE entry_id=?",
        (new_key, old_key),
    )
    conn.execute("DELETE FROM embeddings WHERE entry_id=?", (old_key,))


def apply_entry_edit(
    conn,
    taxonomy,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
    scope: str,
    expected_version: str,
    new_dimension: str,
    new_topic: str,
    new_scope: str,
    confidence: float,
    status: str,
    summary: str,
    body: str,
    degraded_reason: str | None = None,
) -> dict:
    checked = check_entry_edit(
        conn, taxonomy,
        user_source=user_source, user_id=user_id, entry_id=entry_id, scope=scope,
        new_dimension=new_dimension, new_topic=new_topic, new_scope=new_scope,
        confidence=confidence, status=status, summary=summary, body=body,
        degraded_reason=degraded_reason,
    )
    if not checked["ok"]:
        return {**checked, "stale": False, "id": None, "updated_at": None}

    row = _entry_row(
        conn, user_source=user_source, user_id=user_id,
        entry_id=entry_id, scope=scope,
    )
    if row is None:
        return {
            "ok": False, "errors": ["条目不存在或已被删除"], "warnings": [],
            "conflicts": [], "stale": False, "id": None, "updated_at": None,
        }

    new_dimension = (new_dimension or "").strip()
    new_topic = (new_topic or "").strip()
    new_scope = (new_scope or "").strip()
    new_id = entry_key(new_dimension, new_topic, new_scope)
    now = utcnow()
    archived_at = row["archived_at"]
    if status == "archived":
        archived_at = archived_at or now
    else:
        archived_at = None
    superseded_by = row["superseded_by"] if status == "archived" else None

    current = conn.execute(
        "UPDATE entries SET dimension=?, topic=?, scope=?, id=?, confidence=?,"
        " status=?, body=?, summary=?, degraded_reason=?, archived_at=?,"
        " superseded_by=?, topic_new=NULL, updated_at=?"
        " WHERE user_source=? AND user_id=? AND id=? AND scope=? AND updated_at=?",
        (
            new_dimension, new_topic, new_scope, new_id, confidence, status,
            body.strip(), summary.strip(), (degraded_reason or "").strip() or None,
            archived_at, superseded_by, now,
            user_source, user_id, entry_id, scope, expected_version,
        ),
    )
    if current.rowcount == 0:
        return {
            "ok": False, "errors": ["条目已变化，请刷新后重试"], "warnings": [],
            "conflicts": [], "stale": True, "id": None, "updated_at": None,
        }

    if new_id != entry_id:
        _rename_references(
            conn,
            user_source=user_source, user_id=user_id,
            old_id=entry_id, old_scope=scope, new_id=new_id,
        )

    _sync_embedding(
        conn,
        user_source=user_source, user_id=user_id,
        old_dimension=row["dimension"], old_topic=row["topic"], old_scope=row["scope"],
        new_dimension=new_dimension, new_topic=new_topic, new_scope=new_scope,
        body_changed=body.strip() != row["body"],
    )

    return {
        "ok": True,
        "errors": [],
        "warnings": checked["warnings"],
        "conflicts": [],
        "stale": False,
        "id": new_id,
        "updated_at": now,
    }
