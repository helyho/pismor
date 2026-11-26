"""跨用户条目检索与编辑。"""
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pismor.admin.audit import record_admin_audit
from pismor.admin.deps import get_settings, get_taxonomy, require
from pismor.admin.entry_service import apply_entry_edit, check_entry_edit
from pismor.admin.queries.entries import get_entry_facets, list_entries
from pismor.admin.queries.review import get_entry_detail
from pismor.admin.schemas import (
    EntryEditCheckOut,
    EntryEditIn,
    EntryEditOut,
    EntryFacetsOut,
    EntryListOut,
    ReviewDetailOut,
)
from pismor.core.db import get_conn


router = APIRouter(prefix="/admin/api/entries", tags=["admin-entries"])


@router.get("", response_model=EntryListOut)
def entries(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    dimension: str | None = None,
    status: str | None = None,
    user_source: str | None = None,
    user_id: str | None = None,
    scope: str | None = None,
    topic: str | None = None,
    q: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
    order: Literal["updated_desc", "updated_asc", "confidence_desc"] = "updated_desc",
    _principal=Depends(require("profile.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return list_entries(
            conn,
            page=page,
            page_size=page_size,
            dimension=dimension,
            status=status,
            user_source=user_source,
            user_id=user_id,
            scope=scope,
            topic=topic,
            q=q,
            confidence_min=confidence_min,
            confidence_max=confidence_max,
            updated_from=updated_from,
            updated_to=updated_to,
            order=order,
        )


@router.get("/facets", response_model=EntryFacetsOut)
def facets(request: Request, _principal=Depends(require("profile.read"))):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return get_entry_facets(conn)


@router.get("/detail", response_model=ReviewDetailOut)
def entry_detail(
    request: Request,
    user_source: str,
    user_id: str,
    entry_id: str,
    scope: str,
    _principal=Depends(require("profile.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        detail = get_entry_detail(
            conn,
            user_source=user_source,
            user_id=user_id,
            entry_id=entry_id,
            scope=scope,
        )
    if detail is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return detail


@router.post("/validate-edit", response_model=EntryEditCheckOut)
def validate_edit(
    payload: EntryEditIn,
    request: Request,
    _principal=Depends(require("profile.write")),
):
    settings = get_settings(request)
    taxonomy = get_taxonomy(request)
    with closing(get_conn(settings.db_path)) as conn:
        return check_entry_edit(
            conn,
            taxonomy,
            user_source=payload.user_source,
            user_id=payload.user_id,
            entry_id=payload.entry_id,
            scope=payload.scope,
            new_dimension=payload.new_dimension,
            new_topic=payload.new_topic,
            new_scope=payload.new_scope,
            confidence=payload.confidence,
            status=payload.status,
            summary=payload.summary,
            body=payload.body,
            degraded_reason=payload.degraded_reason,
        )


@router.patch("", response_model=EntryEditOut)
def edit_entry(
    payload: EntryEditIn,
    request: Request,
    principal=Depends(require("profile.write")),
):
    settings = get_settings(request)
    taxonomy = get_taxonomy(request)
    with closing(get_conn(settings.db_path)) as conn:
        with conn:
            result = apply_entry_edit(
                conn,
                taxonomy,
                user_source=payload.user_source,
                user_id=payload.user_id,
                entry_id=payload.entry_id,
                scope=payload.scope,
                expected_version=payload.expected_version,
                new_dimension=payload.new_dimension,
                new_topic=payload.new_topic,
                new_scope=payload.new_scope,
                confidence=payload.confidence,
                status=payload.status,
                summary=payload.summary,
                body=payload.body,
                degraded_reason=payload.degraded_reason,
            )
            if not result["ok"]:
                status_code = 409 if result.get("stale") or result.get("conflicts") else 422
                raise HTTPException(
                    status_code=status_code,
                    detail={
                        "errors": result["errors"],
                        "warnings": result["warnings"],
                        "conflicts": result["conflicts"],
                    },
                )
            record_admin_audit(
                conn,
                principal,
                "entry.edit",
                result["id"],
                {
                    "old_id": payload.entry_id,
                    "old_scope": payload.scope,
                    "new_id": result["id"],
                    "new_scope": payload.new_scope,
                    "status": payload.status,
                    "dimension": payload.new_dimension,
                    "topic": payload.new_topic,
                },
            )
            return {
                "ok": True,
                "id": result["id"],
                "updated_at": result["updated_at"],
                "warnings": result["warnings"],
            }
