"""跨用户条目检索与编辑。"""
import logging
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pismor.admin.audit import record_admin_audit
from pismor.admin.deps import get_settings, get_taxonomy, require
from pismor.admin.entry_service import (
    EntryExistsError,
    SafetyRejectedError,
    UserDeletedError,
    apply_entry_edit,
    check_entry_create,
    check_entry_edit,
    create_entry,
)
from pismor.admin.queries.entries import get_entry_facets, list_entries
from pismor.admin.queries.review import get_entry_detail
from pismor.admin.schemas import (
    EntryCreateIn,
    EntryCreateOut,
    EntryEditCheckOut,
    EntryEditIn,
    EntryEditOut,
    EntryFacetsOut,
    EntryListOut,
    EntryValidateOut,
    ReviewDetailOut,
)
from pismor.core.db import get_conn
from pismor.core.keys import display_key
from pismor.pipeline import writer
from pismor.pipeline.resolver import LlmUnavailable, neighbor_candidates


logger = logging.getLogger("mem.admin.entries")

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
    topic: str | None = None,
    q: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
    order: Literal["updated_desc", "updated_asc", "confidence_desc"] = "updated_desc",
    _principal=Depends(require("memory.read")),
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
            topic=topic,
            q=q,
            confidence_min=confidence_min,
            confidence_max=confidence_max,
            updated_from=updated_from,
            updated_to=updated_to,
            order=order,
        )


@router.get("/facets", response_model=EntryFacetsOut)
def facets(request: Request, _principal=Depends(require("memory.read"))):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return get_entry_facets(conn)


@router.get("/detail", response_model=ReviewDetailOut)
def entry_detail(
    request: Request,
    user_source: str,
    user_id: str,
    entry_id: str,
    _principal=Depends(require("memory.read")),
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
    return detail


@router.post("/validate-edit", response_model=EntryEditCheckOut)
def validate_edit(
    payload: EntryEditIn,
    request: Request,
    _principal=Depends(require("memory.write")),
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
            new_dimension=payload.new_dimension,
            new_topic=payload.new_topic,
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
    principal=Depends(require("memory.write")),
):
    settings = get_settings(request)
    taxonomy = get_taxonomy(request)
    embedder = getattr(request.app.state, "embedder", None)
    with closing(get_conn(settings.db_path)) as conn:
        # 向量在写事务外预计算（writer 的既有原则：网络 IO 不持有 WAL 写锁）
        current = conn.execute(
            "SELECT body FROM entries WHERE user_source=? AND user_id=? AND id=?",
            (payload.user_source, payload.user_id, payload.entry_id),
        ).fetchone()
        body_changed = current is not None and payload.body.strip() != current["body"]
        vector = writer.compute_vector(embedder, payload.body) if body_changed else None
        with conn:
            result = apply_entry_edit(
                conn,
                taxonomy,
                user_source=payload.user_source,
                user_id=payload.user_id,
                entry_id=payload.entry_id,
                expected_version=payload.expected_version,
                new_dimension=payload.new_dimension,
                new_topic=payload.new_topic,
                confidence=payload.confidence,
                status=payload.status,
                summary=payload.summary,
                body=payload.body,
                degraded_reason=payload.degraded_reason,
                vector=vector,
                vector_requested=embedder is not None,
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
                display_key(result["dimension"], result["topic"]),
                {
                    "old_id": payload.entry_id,
                    "new_id": result["id"],
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


def _audit_rejected(conn, principal, payload, reason: str, target: str) -> None:
    """拒绝创建留痕：条目未写，audit 失败只记日志（不拖垮 4xx 响应）。"""
    try:
        record_admin_audit(
            conn,
            principal,
            "entry.create.rejected",
            target,
            {
                "user_source": payload.user_source,
                "user_id": payload.user_id,
                "dimension": payload.dimension,
                "topic": payload.topic,
                "reason": reason,
            },
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.warning("admin audit (entry.create.rejected) failed; reason=%s", reason)


def _field_errors(errors) -> list[dict]:
    return [asdict(e) if hasattr(e, "field") else e for e in errors]


@router.post("/validate-create", response_model=EntryValidateOut)
def validate_create(
    payload: EntryCreateIn,
    request: Request,
    _principal=Depends(require("memory.write")),
):
    """创建前只读校验：绝不写入；LLM 缺失且有近邻候选时预警提交将被拒绝。"""
    settings = get_settings(request)
    taxonomy = get_taxonomy(request)
    llm = getattr(request.app.state, "llm", None)
    with closing(get_conn(settings.db_path)) as conn:
        checked = check_entry_create(
            conn,
            taxonomy,
            user_source=payload.user_source,
            user_id=payload.user_id,
            dimension=payload.dimension,
            topic=payload.topic,
            summary=payload.summary,
            body=payload.body,
            quote=payload.quote,
            confidence=payload.confidence,
        )
        warnings = list(checked.warnings)
        if checked.ok and neighbor_candidates(
                conn, payload.user_source, payload.user_id, payload.dimension
        ) and llm is None:
            warnings.append("提交时需要语义互斥检查，该服务当前不可用，提交将被拒绝")
        return {
            # 与编辑校验同语义：ok = 无字段错误且无同键冲突（conflicts 由前端展示）
            "ok": checked.ok and not checked.conflicts,
            "normalized_topic": checked.normalized_topic,
            "errors": _field_errors(checked.errors),
            "warnings": warnings,
            "conflicts": checked.conflicts,
        }


@router.post("", response_model=EntryCreateOut, status_code=201)
def create_entry_endpoint(
    payload: EntryCreateIn,
    request: Request,
    principal=Depends(require("memory.write")),
):
    settings = get_settings(request)
    taxonomy = get_taxonomy(request)
    embedder = getattr(request.app.state, "embedder", None)
    llm = getattr(request.app.state, "llm", None)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="microseconds")
    origin_ref = payload.origin_ref or (
        f"admin:{principal.subject}:{int(now_dt.timestamp())}")
    with closing(get_conn(settings.db_path)) as conn:
        try:
            result = create_entry(
                conn,
                taxonomy,
                user_source=payload.user_source,
                user_id=payload.user_id,
                dimension=payload.dimension,
                topic=payload.topic,
                summary=payload.summary,
                body=payload.body,
                quote=payload.quote,
                confidence=payload.confidence,
                origin_ref=origin_ref,
                embedder=embedder,
                llm=llm,
                now=now,
            )
        except SafetyRejectedError as exc:
            # write_rejected 是写入层基建失败（applied 为空），与安全规则拒绝分开留痕
            reason = ("write_rejected"
                      if exc.errors and exc.errors[0].code == "write_rejected"
                      else "safety_rejected")
            _audit_rejected(conn, principal, payload, reason,
                            display_key(payload.dimension, payload.topic))
            raise HTTPException(
                status_code=422,
                detail={"errors": _field_errors(exc.errors)},
            ) from exc
        except LlmUnavailable:
            _audit_rejected(conn, principal, payload, "llm_unavailable",
                            display_key(payload.dimension, payload.topic))
            return JSONResponse(status_code=503, content={
                "ok": False,
                "code": "llm_unavailable",
                "message": "语义互斥检查服务暂时不可用，记忆未保存，请稍后重试",
            })
        except EntryExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail={"errors": [exc.message], "existing": exc.existing},
            ) from exc
        except UserDeletedError as exc:
            raise HTTPException(
                status_code=409,
                detail={"errors": [str(exc)]},
            ) from exc
        if not result["ok"]:
            raise HTTPException(
                status_code=400,
                detail={
                    "errors": _field_errors(result["errors"]),
                    "warnings": result["warnings"],
                },
            )
        # 审计在 apply_actions 自身事务之后：留痕失败只记日志，不回滚记忆
        try:
            record_admin_audit(
                conn,
                principal,
                "entry.create",
                display_key(result["dimension"], result["topic"]),
                {
                    "user_source": payload.user_source,
                    "user_id": payload.user_id,
                    "dimension": result["dimension"],
                    "topic": result["topic"],
                    "status": result["status"],
                    "confidence": result["confidence"],
                    "origin_type": "admin_manual",
                    "origin_ref": origin_ref,
                },
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            logger.warning("admin audit (entry.create) failed; id=%s", result["id"])
        return result


@router.get("/topics")
def topics(request: Request, _principal=Depends(require("memory.read"))):
    """词表可用主题（按维度分组）：排除 disabled_topics，供录入表单下拉。"""
    taxonomy = get_taxonomy(request)
    return {
        dim.key: [t for t in (dim.topics or [])
                  if t not in (dim.disabled_topics or [])]
        for dim in taxonomy.dimensions.values()
    }
