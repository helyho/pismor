"""管理后台危险操作留痕。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pismor.admin.deps import Principal


def record_actor_audit(
    conn,
    actor: str,
    action: str,
    target: str,
    detail: Any = None,
) -> None:
    """按原始 actor 写入审计；公开 API 无 Principal 时使用。"""
    conn.execute(
        "INSERT INTO admin_audit (actor, action, target, ts, detail) VALUES (?, ?, ?, ?, ?)",
        (
            actor,
            action,
            target,
            datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None,
        ),
    )


def record_admin_audit(
    conn,
    principal: Principal,
    action: str,
    target: str,
    detail: Any = None,
) -> None:
    record_actor_audit(conn, principal.subject, action, target, detail)
