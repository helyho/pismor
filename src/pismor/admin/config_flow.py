"""配置保存/恢复编排：校验 → 写盘 → 重新加载 → 发布 → 审计。

settings 与 taxonomy 共用这一套顺序契约；路由只保留协议、鉴权与
错误映射。写盘失败时的回滚语义由 write_config 的原子写保证。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pismor.core.config_files import (
    ConfigFileNotFound,
    ConfigInvalid,
    ConfigVersionConflict,
    as_plain,
    backup_path,
    dump_yaml,
    merge_roundtrip,
    read_yaml,
    roundtrip_yaml,
    write_config,
)


class ConfigRejected(Exception):
    """校验未通过；report 原样交给路由映射 422。"""

    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.message = message
        self.report = report


class ConfigFailure(Exception):
    """校验之后的写盘或重新加载失败。"""


class ConfigBackupNotFound(Exception):
    """备份 id 不存在或不可读。"""


@dataclass(frozen=True)
class ConfigTarget:
    kind: str                 # settings / taxonomy，用于审计 action 前缀
    noun: str                 # 运行配置 / 词表，用于错误文案
    path: Path
    backup_root: Path
    backup_keep: int
    label: str
    rollback_label: str
    validate: Callable[[dict], dict]
    load: Callable[[Path], Any]
    publish: Callable[[Any], None]
    audit: Callable[[str, str, dict], None]
    applied: Callable[[dict], list[str]] | None = None
    restart_required: Callable[[dict], list[str]] | None = None


def _report_list(target: ConfigTarget, report: dict, name: str) -> list[str]:
    override = getattr(target, name)
    if override is not None:
        return list(override(report))
    return list(report.get(name, []))


def save(
    target: ConfigTarget,
    *,
    raw: dict,
    yaml_text: str | None,
    expected_version: str | None,
) -> dict:
    report = target.validate(as_plain(raw))
    if not report["ok"]:
        raise ConfigRejected(f"{target.noun}校验失败", report)
    text = yaml_text
    if text is None:
        rt_data, _old_text, _version = read_yaml(target.path)
        text = dump_yaml(merge_roundtrip(rt_data, raw))
    try:
        version = write_config(
            target.path,
            text,
            expected_version=expected_version,
            backup_root=target.backup_root,
            keep=target.backup_keep,
            label=target.label,
        )
        value = target.load(target.path)
    except (ConfigVersionConflict, ConfigFileNotFound):
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConfigFailure(f"{target.noun}写入失败: {exc}") from exc
    target.publish(value)
    applied = _report_list(target, report, "applied")
    restart = _report_list(target, report, "restart_required")
    target.audit(
        f"{target.kind}.save",
        str(target.path),
        {
            "version": version,
            "applied": applied,
            "restart_required": restart,
            "warnings": report.get("warnings", []),
        },
    )
    return {
        "ok": True,
        "version": version,
        "applied": applied,
        "restart_required": restart,
        "warnings": report.get("warnings", []),
        "backup": None,
    }


def restore(target: ConfigTarget, *, backup_id: str) -> dict:
    try:
        src = backup_path(target.backup_root, backup_id)
    except (ConfigFileNotFound, ConfigInvalid) as exc:
        raise ConfigBackupNotFound(backup_id) from exc
    text = src.read_text(encoding="utf-8")
    raw = roundtrip_yaml().load(text) or {}
    report = target.validate(as_plain(raw))
    if not report["ok"]:
        raise ConfigRejected(f"备份{target.noun}校验失败", report)
    try:
        version = write_config(
            target.path,
            text,
            backup_root=target.backup_root,
            keep=target.backup_keep,
            label=target.rollback_label,
        )
        value = target.load(target.path)
    except Exception as exc:  # noqa: BLE001
        raise ConfigFailure(f"{target.noun}恢复失败: {exc}") from exc
    target.publish(value)
    target.audit(
        f"{target.kind}.restore",
        str(target.path),
        {"backup": backup_id, "version": version},
    )
    return {"ok": True, "version": version, "restored": backup_id}
