from contextlib import closing

from fastapi import APIRouter, Depends, Query, Request

from pismor.admin.deps import Principal, get_settings, require
from pismor.admin.queries.system import (
    get_health,
    get_stats,
    list_admin_audit,
    list_jobs,
)
from pismor.admin.schemas import (
    AdminAuditOut,
    JobRunOut,
    SystemHealthOut,
    SystemStatsOut,
)
from pismor.core.db import get_conn


router = APIRouter(prefix="/admin/api/system", tags=["admin-system"])


@router.get("/health", response_model=SystemHealthOut)
def health(request: Request, principal: Principal = Depends(require("system.read"))):
    settings = get_settings(request)
    try:
        with closing(get_conn(settings.db_path)) as conn:
            return get_health(conn, settings.db_path)
    except Exception as exc:
        return {
            "db_ok": False,
            "integrity": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }


@router.get("/jobs", response_model=list[JobRunOut])
def jobs(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    principal: Principal = Depends(require("system.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return list_jobs(conn, limit)


@router.get("/stats", response_model=SystemStatsOut)
def stats(request: Request, principal: Principal = Depends(require("system.read"))):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return get_stats(conn)


@router.get("/audit", response_model=list[AdminAuditOut])
def audit(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require("system.read")),
):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return list_admin_audit(conn, limit)
