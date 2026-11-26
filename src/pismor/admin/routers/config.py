"""运行配置：注释、校验、保存、备份、恢复。"""
from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from pismor.admin.audit import record_admin_audit
from pismor.admin.config_service import settings_to_data, validate_settings
from pismor.admin.deps import Principal, get_settings, require
from pismor.admin.schemas import ConfigBackupOut, ConfigValidateIn, ConfigWriteIn, ConfigWriteOut
from pismor.core.comments import collect_comments_from_data
from pismor.core.config import parse_settings
from pismor.core.db import get_conn
from pismor.core.config_files import (
    ConfigFileNotFound,
    ConfigInvalid,
    ConfigVersionConflict,
    as_plain,
    backup_path,
    dump_yaml,
    list_backups,
    merge_roundtrip,
    read_yaml,
    roundtrip_yaml,
    write_config,
)

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


@router.get("")
def get_config(request: Request, _=Depends(require("settings.read"))):
    settings = get_settings(request)
    path = settings.config_path
    if path is None:
        raise HTTPException(status_code=404, detail="settings.yaml 路径未配置")
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
    path = settings.config_path
    if path is None:
        raise HTTPException(status_code=404, detail="settings.yaml 路径未配置")
    raw, yaml_text = _parse_payload(payload)
    report = validate_settings(raw, settings, config_path=path)
    if not report["ok"]:
        raise HTTPException(
            status_code=422,
            detail={"message": "运行配置校验失败", "errors": report["errors"], "warnings": report["warnings"]},
        )
    if yaml_text is None:
        rt_data, _old_text, _version = read_yaml(path)
        merged = merge_roundtrip(rt_data, raw)
        yaml_text = dump_yaml(merged)
    backup_root = settings.backup.root / "config"
    try:
        version = write_config(
            path,
            yaml_text,
            expected_version=payload.expected_version,
            backup_root=backup_root,
            keep=settings.backup.keep,
            label="settings",
        )
        config, _text, _new_version = read_yaml(path)
        new_settings = parse_settings(as_plain(config), path=path)
    except ConfigVersionConflict:
        raise HTTPException(status_code=409, detail="运行配置已被其他操作修改，请刷新后重试")
    except ConfigFileNotFound:
        raise HTTPException(status_code=404, detail=f"配置文件不存在: {path}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"运行配置写入失败: {exc}")
    store.replace(new_settings)
    request.app.state.settings = new_settings
    _audit(settings, principal, "settings.save", str(path), {
        "version": version,
        "applied": report["applied"],
        "restart_required": report["restart_required"],
    })
    return ConfigWriteOut(
        ok=True,
        version=version,
        applied=report["applied"],
        restart_required=report["restart_required"],
        warnings=report["warnings"],
        backup=None,
    ).model_dump()


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
    path = settings.config_path
    if path is None:
        raise HTTPException(status_code=404, detail="settings.yaml 路径未配置")
    root = settings.backup.root / "config"
    try:
        src = backup_path(root, backup_id)
    except (ConfigFileNotFound, ConfigInvalid):
        raise HTTPException(status_code=404, detail="备份不存在")
    text = src.read_text(encoding="utf-8")
    raw = roundtrip_yaml().load(text)
    if raw is None:
        raw = {}
    report = validate_settings(as_plain(raw), settings, config_path=path)
    if not report["ok"]:
        raise HTTPException(
            status_code=422,
            detail={"message": "备份配置校验失败", "errors": report["errors"]},
        )
    try:
        version = write_config(
            path,
            text,
            backup_root=root,
            keep=settings.backup.keep,
            label="settings-rollback",
        )
        config, _new_text, _v = read_yaml(path)
        new_settings = parse_settings(as_plain(config), path=path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"运行配置恢复失败: {exc}")
    store.replace(new_settings)
    request.app.state.settings = new_settings
    _audit(settings, principal, "settings.restore", str(path), {"backup": backup_id, "version": version})
    return {"ok": True, "version": version, "restored": backup_id}
