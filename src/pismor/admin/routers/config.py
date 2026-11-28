"""运行配置：注释、校验、保存、备份、恢复。"""
from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from pismor.admin import config_flow
from pismor.admin.audit import record_admin_audit
from pismor.admin.config_service import settings_to_data, validate_settings
from pismor.admin.deps import Principal, get_settings, require
from pismor.admin.schemas import ConfigBackupOut, ConfigValidateIn, ConfigWriteIn, ConfigWriteOut
from pismor.core.comments import collect_comments_from_data
from pismor.core.config import parse_settings
from pismor.core.config_files import (
    ConfigFileNotFound,
    ConfigVersionConflict,
    as_plain,
    list_backups,
    read_yaml,
    roundtrip_yaml,
)
from pismor.core.db import get_conn

router = APIRouter(prefix="/admin/api/config/settings", tags=["admin-config"])


def _parse_payload(payload: ConfigValidateIn | ConfigWriteIn):
    if payload.yaml is not None:
        raw = roundtrip_yaml().load(payload.yaml)
        if raw is None:
            raw = {}
        return raw, payload.yaml
    if payload.data is not None:
        return payload.data, None
    raise HTTPException(status_code=422, detail="yaml 或 data 必须提供一个")


def _audit(settings, principal: Principal, action: str, target: str, detail) -> None:
    try:
        with closing(get_conn(settings.db_path)) as conn:
            with conn:
                record_admin_audit(conn, principal, action, target, detail)
    except Exception:  # 审计失败不阻断配置保存
        pass


def _target(request: Request, settings, principal: Principal) -> config_flow.ConfigTarget:
    path = settings.config_path

    def publish(new) -> None:
        request.app.state.settings_store.replace(new)
        request.app.state.settings = new

    return config_flow.ConfigTarget(
        kind="settings",
        noun="运行配置",
        path=path,
        backup_root=settings.backup.root / "config",
        backup_keep=settings.backup.keep,
        label="settings",
        rollback_label="settings-rollback",
        validate=lambda raw: validate_settings(raw, settings, config_path=path),
        load=lambda p: parse_settings(as_plain(read_yaml(p)[0]), path=p),
        publish=publish,
        audit=lambda action, tgt, detail: _audit(settings, principal, action, tgt, detail),
    )


def _require_path(settings) -> None:
    if settings.config_path is None:
        raise HTTPException(status_code=404, detail="settings.yaml 路径未配置")


@router.get("")
def get_config(request: Request, _=Depends(require("settings.read"))):
    settings = get_settings(request)
    _require_path(settings)
    path = settings.config_path
    try:
        data, text, version = read_yaml(path)
    except ConfigFileNotFound:
        raise HTTPException(status_code=404, detail=f"配置文件不存在: {path}")
    return {
        "version": version,
        "path": str(path),
        "yaml": text,
        "data": as_plain(data),
        "comments": collect_comments_from_data(data),
        "settings": settings_to_data(settings),
    }


@router.post("/validate")
def validate(
    request: Request,
    payload: ConfigValidateIn,
    _=Depends(require("settings.read")),
):
    settings = get_settings(request)
    raw, _text = _parse_payload(payload)
    return validate_settings(raw, settings, config_path=settings.config_path)


@router.put("")
def save(
    request: Request,
    payload: ConfigWriteIn,
    principal: Principal = Depends(require("settings.write")),
):
    store = request.app.state.settings_store
    settings = store.current()
    _require_path(settings)
    path = settings.config_path
    raw, yaml_text = _parse_payload(payload)
    try:
        result = config_flow.save(
            _target(request, settings, principal),
            raw=raw,
            yaml_text=yaml_text,
            expected_version=payload.expected_version,
        )
    except config_flow.ConfigRejected as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": exc.message,
                "errors": exc.report["errors"],
                "warnings": exc.report["warnings"],
            },
        )
    except ConfigVersionConflict:
        raise HTTPException(status_code=409, detail="运行配置已被其他操作修改，请刷新后重试")
    except ConfigFileNotFound:
        raise HTTPException(status_code=404, detail=f"配置文件不存在: {path}")
    except config_flow.ConfigFailure as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return ConfigWriteOut(**result).model_dump()


@router.get("/backups", response_model=list[ConfigBackupOut])
def backups(request: Request, _=Depends(require("settings.read"))):
    settings = get_settings(request)
    return [
        ConfigBackupOut(**{**item, "kind": "settings"})
        for item in list_backups(settings.backup.root / "config")
        if item["name"].startswith("settings.")
    ]


@router.post("/backups/{backup_id}/restore")
def restore(
    request: Request,
    backup_id: str,
    principal: Principal = Depends(require("settings.write")),
):
    store = request.app.state.settings_store
    settings = store.current()
    _require_path(settings)
    try:
        return config_flow.restore(_target(request, settings, principal), backup_id=backup_id)
    except config_flow.ConfigBackupNotFound:
        raise HTTPException(status_code=404, detail="备份不存在")
    except config_flow.ConfigRejected as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": exc.message, "errors": exc.report["errors"]},
        )
    except config_flow.ConfigFailure as exc:
        raise HTTPException(status_code=422, detail=str(exc))
