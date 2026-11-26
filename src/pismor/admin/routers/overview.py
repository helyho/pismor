from contextlib import closing

from fastapi import APIRouter, Depends, Request

from pismor.admin.deps import Principal, get_principal, require, get_settings
from pismor.admin.queries.overview import get_overview
from pismor.admin.schemas import OverviewOut, PrincipalOut
from pismor.core.db import get_conn


router = APIRouter(prefix="/admin/api", tags=["admin"])


@router.get("/me", response_model=PrincipalOut)
def me(principal: Principal = Depends(get_principal)):
    return {
        "subject": principal.subject,
        "display_name": principal.display_name,
        "roles": list(principal.roles),
        "capabilities": sorted(principal.capabilities),
    }


@router.get("/overview", response_model=OverviewOut)
def overview(request: Request, principal: Principal = Depends(require("system.read"))):
    settings = get_settings(request)
    with closing(get_conn(settings.db_path)) as conn:
        return get_overview(conn, settings.db_path)
