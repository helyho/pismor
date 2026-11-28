"""配置文件读写：round-trip YAML、版本校验、原子写、备份与恢复。

- round-trip 保留注释、键顺序与引号风格；写回不会把管理后台的说明注释洗掉。
- version = sha256 前 12 位 + mtime_ns + size；调用方用它做乐观锁，失配返回 409。
- 写文件统一走临时文件 + os.replace；同时用文件锁串行化管理路由与日批写回。
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


class ConfigFileNotFound(FileNotFoundError):
    pass


class ConfigVersionConflict(RuntimeError):
    pass


class ConfigInvalid(RuntimeError):
    pass


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    y.representer.add_representer(
        type(None),
        lambda representer, _data: representer.represent_scalar(
            "tag:yaml.org,2002:null", "~"
        ),
    )
    return y


def roundtrip_yaml() -> YAML:
    """公开的 round-trip YAML 实例（保留注释/引号/键顺序）。"""
    return _yaml()


def merge_roundtrip(target: Any, incoming: Any) -> Any:
    """把普通 JSON 数据合并进 round-trip 数据，尽量保留原字段注释。

    - mapping：按 incoming 的键增删改；同名 mapping 递归。
    - sequence：长度一致且元素都是 mapping 时按下标递归（维度模板）；否则整体替换。
    """
    if isinstance(target, CommentedMap) and isinstance(incoming, dict):
        for key in list(target.keys()):
            if key not in incoming:
                del target[key]
        for key, value in incoming.items():
            if key in target:
                target[key] = merge_roundtrip(target[key], value)
            else:
                target[key] = value
        return target
    if (
        isinstance(target, CommentedSeq)
        and isinstance(incoming, list)
        and len(target) == len(incoming)
        and all(isinstance(item, dict) for item in incoming)
        and all(isinstance(item, CommentedMap) for item in target)
    ):
        for idx, value in enumerate(incoming):
            target[idx] = merge_roundtrip(target[idx], value)
        return target
    return incoming


def _stat_version(text: str, path: Path) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    try:
        st = path.stat()
        return f"{digest}:{st.st_mtime_ns}:{st.st_size}"
    except FileNotFoundError:
        return f"{digest}:0:0"


def file_version(path: Path) -> str:
    if not path.exists():
        raise ConfigFileNotFound(str(path))
    text = path.read_text(encoding="utf-8")
    return _stat_version(text, path)


def read_yaml(path: Path) -> tuple[Any, str, str]:
    """返回 (round-trip 数据, 原始文本, 版本)。"""
    if not path.exists():
        raise ConfigFileNotFound(str(path))
    text = path.read_text(encoding="utf-8")
    data = _yaml().load(text)
    if data is None:
        data = CommentedMap()
    return data, text, _stat_version(text, path)


def dump_yaml(data: Any) -> str:
    import io

    stream = io.StringIO()
    _yaml().dump(data, stream)
    return stream.getvalue()


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """跨进程文件锁；锁文件与目标同目录，写完保留（很小的空文件）。"""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "backup"


def backup_config(
    path: Path,
    backup_root: Path,
    *,
    keep: int = 30,
    label: str = "",
) -> str | None:
    """备份当前文件，返回备份文件名；文件不存在时返回 None。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    version = _stat_version(text, path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    tag = _safe_name(label) if label else "save"
    # 形如 settings.20260101T....<hash>.settings.yaml；第三段固定为版本，便于历史页展示。
    name = f"{path.stem}.{stamp}.{version.split(':')[0]}.{tag}{path.suffix}"
    backup_root.mkdir(parents=True, exist_ok=True)
    target = backup_root / name
    write_text_atomic(target, text)
    prune_backups(backup_root, keep=keep)
    return name


def prune_backups(backup_root: Path, *, keep: int) -> None:
    if keep < 0:
        raise ValueError("keep must be >= 0")
    files = sorted(
        [p for p in backup_root.glob("*.y*ml") if p.is_file()],
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in files[keep:]:
        try:
            stale.unlink()
        except FileNotFoundError:
            pass


def list_backups(backup_root: Path) -> list[dict]:
    if not backup_root.is_dir():
        return []
    items = []
    for p in backup_root.glob("*.y*ml"):
        if not p.is_file():
            continue
        st = p.stat()
        parts = p.stem.split(".")
        version = parts[2][:12] if len(parts) >= 3 else ""
        items.append(
            {
                "id": p.name,
                "name": p.name,
                "size": st.st_size,
                "updated_at": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
                "version": version,
            }
        )
    return sorted(items, key=lambda item: item["updated_at"], reverse=True)


def backup_path(backup_root: Path, backup_id: str) -> Path:
    name = Path(backup_id).name
    if name != backup_id or name in {"", ".", ".."}:
        raise ConfigInvalid("invalid backup id")
    path = backup_root / name
    if not path.is_file():
        raise ConfigFileNotFound(str(path))
    return path


def write_config(
    path: Path,
    text: str,
    *,
    expected_version: str | None = None,
    backup_root: Path | None = None,
    keep: int = 30,
    label: str = "",
) -> str:
    """原子写配置；expected_version 失配抛 ConfigVersionConflict。返回新版本。"""
    with file_lock(path):
        if expected_version is not None:
            current = file_version(path)
            if current != expected_version:
                raise ConfigVersionConflict(
                    f"version mismatch: expected {expected_version}, got {current}"
                )
        if backup_root is not None:
            backup_config(path, backup_root, keep=keep, label=label)
        write_text_atomic(path, text)
        return file_version(path)


def as_plain(data: Any) -> Any:
    """CommentMap/CommentedSeq → 普通 dict/list，便于 JSON 响应。"""
    if isinstance(data, CommentedMap):
        return {str(k): as_plain(v) for k, v in data.items()}
    if isinstance(data, CommentedSeq):
        return [as_plain(v) for v in data]
    return data
