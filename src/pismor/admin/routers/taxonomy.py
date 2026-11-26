"""词表配置：注释、校验、保存、备份、恢复。"""
from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from pismor.admin.audit import record_admin_audit
from pismor.admin.config_service import validate_taxonomy
from pismor.admin.deps import Principal, get_settings, require
from pismor.admin.schemas import ConfigBackupOut, ConfigValidateIn, ConfigWriteIn, ConfigWriteOut
from pismor.core.comments import collect_comments_from_data
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
from pismor.core.taxonomy import load_taxonomy

router = APIRouter(prefix="/admin/api/config/taxonomy", tags=["admin-config"])


def _parse_payload(payload: ConfigValidateIn | ConfigWriteIn):
    if payload.yaml is not None:
        raw = roundtrip_yaml().load(payload.yaml)
        if raw is None:
            raw = {}
        return raw, payload.yaml
    if payload.data is not None:
        return payload.data, None
    raise HTTPException(status_code=422, detail="yaml 或 data 必须提供一个")


def _config_path(request: Request):
    return get_settings(request).taxonomy_path


def _audit(settings, principal: Principal, action: str, target: str, detail) -> None:
    try:
        with closing(get_conn(settings.db_path)) as conn:
            with conn:
                record_admin_audit(conn, principal, action, target, detail)
    except Exception:  # 审计失败不阻断词表保存
        pass


@router.get("")
def get_taxonomy(request: Request, _=Depends(require("taxonomy.read"))):
    path = _config_path(request)
    try:
        data, text, version = read_yaml(path)
    except ConfigFileNotFound:
        raise HTTPException(status_code=404, detail=f"词表文件不存在: {path}")
    return {
        "version": version,
        "path": str(path),
        "yaml": text,
        "data": as_plain(data),
        "comments": collect_comments_from_data(data),
    }


@router.post("/validate")
def validate(
    request: Request,
    payload: ConfigValidateIn,
    _=Depends(require("taxonomy.read")),
):
    current = request.app.state.taxonomy_store.current()
    from pismor.core.db import get_conn

    conn = get_conn(get_settings(request).db_path)
    try:
        raw, _text = _parse_payload(payload)
        return validate_taxonomy(raw, current, conn)
    finally:
        conn.close()


@router.put("")
def save(
    request: Request,
    payload: ConfigWriteIn,
    principal: Principal = Depends(require("taxonomy.write")),
):
    settings = get_settings(request)
    path = settings.taxonomy_path
    current = request.app.state.taxonomy_store.current()
    from pismor.core.db import get_conn

    raw, yaml_text = _parse_payload(payload)
    conn = get_conn(settings.db_path)
    try:
        report = validate_taxonomy(raw, current, conn)
    finally:
        conn.close()
    if not report["ok"]:
        raise HTTPException(
            status_code=422,
            detail={"message": "词表校验失败", "errors": report["errors"], "warnings": report["warnings"]},
        )
    backup_root = settings.backup.root / "config"
    if yaml_text is None:
        rt_data, _old_text, _version = read_yaml(path)
        merged = merge_roundtrip(rt_data, raw)
        yaml_text = dump_yaml(merged)
    try:
        version = write_config(
            path,
            yaml_text,
            expected_version=payload.expected_version,
            backup_root=backup_root,
            keep=settings.backup.keep,
            label="taxonomy",
        )
        taxonomy = load_taxonomy(path)
    except ConfigVersionConflict:
        raise HTTPException(status_code=409, detail="词表已被其他操作修改，请刷新后重试")
    except ConfigFileNotFound:
        raise HTTPException(status_code=404, detail=f"词表文件不存在: {path}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"词表写入失败: {exc}")
    request.app.state.taxonomy_store.replace(taxonomy)
    request.app.state.taxonomy = taxonomy
    _audit(settings, principal, "taxonomy.save", str(path), {
        "version": version,
        "warnings": report["warnings"],
    })
    return ConfigWriteOut(
        ok=True,
        version=version,
        applied=["taxonomy"],
        restart_required=[],
        warnings=report["warnings"],
    ).model_dump()


@router.get("/backups", response_model=list[ConfigBackupOut])
def backups(request: Request, _=Depends(require("taxonomy.read"))):
    settings = get_settings(request)
    return [
        ConfigBackupOut(**{**item, "kind": "taxonomy"})
        for item in list_backups(settings.backup.root / "config")
        if item["name"].startswith("taxonomy.")
    ]


@router.post("/backups/{backup_id}/restore")
def restore(
    request: Request,
    backup_id: str,
    principal: Principal = Depends(require("taxonomy.write")),
):
    settings = get_settings(request)
    root = settings.backup.root / "config"
    try:
        src = backup_path(root, backup_id)
    except (ConfigFileNotFound, ConfigInvalid):
        raise HTTPException(status_code=404, detail="备份不存在")
    raw = roundtrip_yaml().load(src.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    from pismor.core.db import get_conn

    conn = get_conn(settings.db_path)
    try:
        report = validate_taxonomy(raw, request.app.state.taxonomy_store.current(), conn)
    finally:
        conn.close()
    if not report["ok"]:
        raise HTTPException(status_code=422, detail={"message": "备份词表校验失败", "errors": report["errors"]})
    try:
        version = write_config(
            settings.taxonomy_path,
            src.read_text(encoding="utf-8"),
            backup_root=root,
            keep=settings.backup.keep,
            label="taxonomy-rollback",
        )
        taxonomy = load_taxonomy(settings.taxonomy_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"词表恢复失败: {exc}")
    request.app.state.taxonomy_store.replace(taxonomy)
    request.app.state.taxonomy = taxonomy
    _audit(settings, principal, "taxonomy.restore", str(settings.taxonomy_path), {
        "backup": backup_id,
        "version": version,
    })
    return {"ok": True, "version": version, "restored": backup_id}
