"""用户列表、删除记录、记忆详情、装配预览与删除。"""
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pismor.admin.audit import record_admin_audit
from pismor.admin.deps import Principal, require, get_settings, get_taxonomy
from pismor.admin.queries.users import get_user_detail, list_tombstones, list_users
from pismor.admin.schemas import (
    AssembleOut,
    DeleteUserOut,
    PurgeUserOut,
    RestoreUserOut,
    TombstoneListOut,
    UserDetailOut,
    UserListOut,
)
from pismor.core.db import get_conn
from pismor.core.keys import valid_scope
from pismor.serving import assembler
from pismor.serving.merge_task import DbMergeTaskSink
from pismor.serving.memory import (
    hard_delete_memory,
    restore_memory,
    soft_delete_memory,
)


router = APIRouter(prefix="/admin/api/users", tags=["admin-users"])


@router.get("", response_model=UserListOut)
def users(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str | None = None,
    user_source: str | None = None,
    order: Literal["recent", "oldest", "entries"] = "recent",
    _principal=Depends(require("memory.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return list_users(
            conn, page=page, page_size=page_size, q=q,
            user_source=user_source, order=order,
        )


@router.get("/tombstones", response_model=TombstoneListOut)
def tombstones(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str | None = None,
    _principal=Depends(require("memory.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return list_tombstones(conn, page=page, page_size=page_size, q=q)


@router.get("/{user_source}/{user_id}", response_model=UserDetailOut)
def user_detail(
    user_source: str,
    user_id: str,
    request: Request,
    _principal=Depends(require("memory.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        detail = get_user_detail(conn, user_source=user_source, user_id=user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="user not found")
    return detail


@router.get("/{user_source}/{user_id}/assemble", response_model=AssembleOut)
def user_assemble(
    user_source: str,
    user_id: str,
    request: Request,
    budget: int = Query(2000, ge=1, le=100000),
    dimensions: str | None = None,
    include_memory: bool = True,
    query: str | None = None,
    scope: str | None = None,
    _principal=Depends(require("memory.read")),
):
    settings = get_settings(request)
    dims = [d.strip() for d in dimensions.split(",") if d.strip()] if dimensions else None
    if scope is not None and not valid_scope(scope):
        raise HTTPException(status_code=422, detail="invalid scope")
    a = settings.assembler
    with closing(get_conn(settings.db_path)) as conn:
        return assembler.assemble(
            conn,
            request.app.state.embedder,
            user_source,
            user_id,
            budget=budget,
            dimensions=dims,
            include_memory=include_memory,
            query=query,
            current_scope=scope,
            audit_sink=DbMergeTaskSink(settings.db_path, user_source, user_id, scope),
            dimension_order=get_taxonomy(request).priority_order(),
            taboo_budget_share=a.taboo_budget_share,
            taboo_merge_threshold=a.taboo_merge_threshold,
            related_top_k=a.related_top_k,
            half_life_days=a.half_life_days,
            clarify_ttl_days=a.clarify_ttl_days,
            min_similarity=settings.embedding.min_similarity,
            write_clarifications=False,
        )


@router.delete("/{user_source}/{user_id}", response_model=DeleteUserOut)
def delete_user(
    user_source: str,
    user_id: str,
    request: Request,
    principal: Principal = Depends(require("memory.delete")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        with conn:
            soft_delete_memory(
                conn, user_source=user_source, user_id=user_id,
                requested_by=principal.subject,
            )
            record_admin_audit(
                conn, principal, "memory.delete", f"{user_source}/{user_id}",
                {"requested_by": principal.subject, "mode": "soft"},
            )
    return {"deleted": True, "mode": "soft"}


@router.post("/{user_source}/{user_id}/restore", response_model=RestoreUserOut)
def restore_user(
    user_source: str,
    user_id: str,
    request: Request,
    principal: Principal = Depends(require("memory.write")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        with conn:
            result = restore_memory(conn, user_source=user_source, user_id=user_id)
            if result is None:
                raise HTTPException(status_code=404, detail="delete record not found")
            if result == "purged":
                raise HTTPException(status_code=409, detail="memory already purged")
            record_admin_audit(
                conn, principal, "memory.restore", f"{user_source}/{user_id}", {},
            )
    return {"restored": True}


@router.delete("/{user_source}/{user_id}/permanent", response_model=PurgeUserOut)
def purge_user(
    user_source: str,
    user_id: str,
    request: Request,
    principal: Principal = Depends(require("memory.delete")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        with conn:
            hard_delete_memory(
                conn, user_source=user_source, user_id=user_id,
                requested_by=principal.subject,
            )
            record_admin_audit(
                conn, principal, "memory.purge", f"{user_source}/{user_id}", {},
            )
    return {"purged": True}
