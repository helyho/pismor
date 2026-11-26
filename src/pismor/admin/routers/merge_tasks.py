"""merge_task 管理接口：超载任务列表与消解（人工归并完成后打卡）。

列表口径与装配无关：只读 merge_tasks，不触发装配；**不要在 GET 里写库**。
消解只改任务状态，不动 entries——实际归并通过 review.decision(topic=...) 完成。
"""
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pismor.admin.audit import record_admin_audit
from pismor.admin.deps import Principal, get_settings, require
from pismor.admin.schemas import (
    MergeTaskListOut,
    MergeTaskOut,
    MergeTaskResolveIn,
)
from pismor.core.db import get_conn
from pismor.serving import merge_task

router = APIRouter(prefix="/admin/api/merge-tasks", tags=["admin-merge-tasks"])


@router.get("", response_model=MergeTaskListOut)
def list_merge_tasks(
    request: Request,
    status: Literal["open", "done", "dismissed"] | None = "open",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _principal=Depends(require("review.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        items, total = merge_task.list_tasks(
            conn, status=status, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.post("/{task_id}/resolve", response_model=MergeTaskOut)
def resolve_merge_task(
    task_id: int,
    payload: MergeTaskResolveIn,
    request: Request,
    principal: Principal = Depends(require("review.decide")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        with conn:
            row = merge_task.resolve(conn, task_id, payload.status, payload.note)
            if row is None:
                raise HTTPException(status_code=404, detail="open task not found")
            record_admin_audit(
                conn, principal, "merge_task.resolve", str(task_id),
                {"status": payload.status, "note": payload.note},
            )
    return row
