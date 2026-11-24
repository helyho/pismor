"""SQLite 热备份：VACUUM INTO 每日快照 + 滚动保留（spec §20），份数由 settings.backup.keep 配置。"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path


def backup(conn: sqlite3.Connection, backup_dir, keep: int = 30) -> Path:
    """快照主库至 backup-YYYYMMDD.db；VACUUM 成功落位后才清理旧快照（同日重跑原子覆盖）。

    先清理上次崩溃遗留的 .tmp（VACUUM INTO 目标必须不存在），失败时也清理本次 tmp，
    避免残留文件把同日重跑变成 "file is not a database"（M-4）。
    """
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"backup-{datetime.now():%Y%m%d}.db"
    tmp_target = target.with_name(target.name + ".tmp")
    for leftover in backup_dir.glob("backup-*.db.tmp"):
        leftover.unlink(missing_ok=True)  # 崩溃遗留的半成品不得阻塞当日快照
    try:
        conn.execute("VACUUM INTO ?", (str(tmp_target),))
        os.replace(tmp_target, target)  # 原子落位：VACUUM 失败不动既有快照
    finally:
        tmp_target.unlink(missing_ok=True)
    snapshots = sorted(backup_dir.glob("backup-*.db"))  # 文件名按日期，字典序即时间序
    for stale in snapshots[:-max(0, keep)]:
        stale.unlink(missing_ok=True)
    return target
