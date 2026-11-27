"""条目级物理删除：预览与执行（管理端）。"""
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from pismor.admin.audit import record_admin_audit
from pismor.admin.deps import get_settings, require
from pismor.admin.schemas import EntryDeleteOut, EntryDeletePreviewOut
from pismor.core.db import get_conn
from pismor.core.keys import display_key
from pismor.serving.purge import preview_entry_delete, purge_entry

router = APIRouter(prefix="/admin/api/entries", tags=["admin-entries"])


@router.get("/delete-preview", response_model=EntryDeletePreviewOut)
def delete_preview(
    request: Request,
    user_source: str,
    user_id: str,
    entry_id: str,
    cascade: bool = True,
    _principal=Depends(require("memory.delete")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        preview = preview_entry_delete(
            conn,
            user_source=user_source,
            user_id=user_id,
            entry_id=entry_id,
            cascade=cascade,
        )
    if preview is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return preview


@router.delete("", response_model=EntryDeleteOut)
def delete_entry(
    request: Request,
    user_source: str,
    user_id: str,
    entry_id: str,
    cascade: bool = True,
    expected_version: str | None = None,
    principal=Depends(require("memory.delete")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        preview = preview_entry_delete(
            conn,
            user_source=user_source,
            user_id=user_id,
            entry_id=entry_id,
            cascade=cascade,
        )
        if preview is None:
            raise HTTPException(status_code=404, detail="entry not found")
        if (expected_version is not None
                and preview["target"]["updated_at"] != expected_version):
            raise HTTPException(
                status_code=409,
                detail={"errors": ["条目已变化，请重新预览"]},
            )
        with conn:
            deleted = purge_entry(
                conn,
                user_source=user_source,
                user_id=user_id,
                entry_id=entry_id,
                cascade_conflicts=cascade,
                expected_version=expected_version,
                taboo_merge_threshold=settings.assembler.taboo_merge_threshold,
            )
            if not deleted:
                raise HTTPException(
                    status_code=409,
                    detail={"errors": ["条目已变化，请重新预览"]},
                )
            record_admin_audit(
                conn,
                principal,
                "entry.delete",
                display_key(preview["target"]["dimension"], preview["target"]["topic"]),
                {
                    "user_source": user_source,
                    "user_id": user_id,
                    "entry_id": entry_id,
                    "variant": preview["target"]["variant"],
                    "status": preview["target"]["status"],
                    "cascade": cascade,
                    "deleted_ids": deleted,
                },
            )
    return {"deleted": deleted}
