"""复核工作台管理 API：队列、筛选面、条目详情、判定。"""
import logging
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pismor.admin.audit import record_admin_audit
from pismor.admin.deps import Principal, require, get_settings, get_taxonomy
from pismor.admin.queries.review import get_entry_detail, get_facets, list_queue
from pismor.admin.schemas import (
    ReviewDecisionIn,
    ReviewDecisionOut,
    ReviewDetailOut,
    ReviewFacetsOut,
    ReviewQueueOut,
)
from pismor.core.db import get_conn
from pismor.serving.review_flow import DecisionRequest, ReviewRuntime, execute_decision

logger = logging.getLogger("mem.admin.review")


router = APIRouter(prefix="/admin/api/review", tags=["admin-review"])


@router.get("/queue", response_model=ReviewQueueOut)
def queue(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    dimension: str | None = None,
    user_source: str | None = None,
    user_id: str | None = None,
    q: str | None = None,
    order: Literal["oldest", "newest"] = "oldest",
    _principal=Depends(require("review.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return list_queue(
            conn,
            page=page,
            page_size=page_size,
            dimension=dimension,
            user_source=user_source,
            user_id=user_id,
            q=q,
            order=order,
        )


@router.get("/facets", response_model=ReviewFacetsOut)
def facets(request: Request, _principal=Depends(require("review.read"))):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return get_facets(conn)


@router.get("/entry", response_model=ReviewDetailOut)
def entry_detail(
    request: Request,
    user_source: str,
    user_id: str,
    entry_id: str,
    _principal=Depends(require("review.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        detail = get_entry_detail(
            conn,
            user_source=user_source,
            user_id=user_id,
            entry_id=entry_id,
        )
    if detail is None:
        raise HTTPException(status_code=404, detail="entry not found")
    # 供复核台把自由 topic 归一到白名单（confirm 的 topic 参数只能在白名单内）
    dim = get_taxonomy(request).dimensions.get(detail["entry"]["dimension"])
    detail["allowed_topics"] = list(getattr(dim, "topics", None) or []) if dim else []
    return detail


@router.post("/decision", response_model=ReviewDecisionOut, response_model_exclude_none=True)
def decide(
    payload: ReviewDecisionIn,
    request: Request,
    principal: Principal = Depends(require("review.decide")),
):
    settings = get_settings(request)
    embedder = getattr(request.app.state, "embedder", None)
    store = getattr(request.app.state, "taxonomy_store", None)

    def audit(conn, result):
        record_admin_audit(
            conn, principal, "review.decision",
            f"{payload.user_source}/{payload.user_id}/{payload.entry_id}",
            {
                "decision": payload.decision,
                "entry_version": payload.entry_version,
                "noop": result["noop"],
            },
        )

    runtime = ReviewRuntime.from_settings(
        settings,
        taxonomy=get_taxonomy(request),
        embedder=embedder,
        taxonomy_store=store,
        in_txn=audit,
    )
    with closing(get_conn(settings.db_path)) as conn:
        return execute_decision(conn, DecisionRequest(**payload.model_dump()), runtime)
