"""SQLite 热备份：VACUUM INTO 每日快照 + 滚动保留 30 份（spec §20）。"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

_KEEP = 30


def backup(conn: sqlite3.Connection, backup_dir) -> Path:
    """快照主库至 backup-YYYYMMDD.db；VACUUM 成功落位后才清理旧快照（同日重跑原子覆盖）。"""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"backup-{datetime.now():%Y%m%d}.db"
    tmp_target = target.with_name(target.name + ".tmp")
    conn.execute("VACUUM INTO ?", (str(tmp_target),))
    os.replace(tmp_target, target)  # 原子落位：VACUUM 失败不动既有快照（无持久性空窗）
    snapshots = sorted(backup_dir.glob("backup-*.db"))  # 文件名按日期，字典序即时间序
    for stale in snapshots[:-_KEEP]:
        stale.unlink(missing_ok=True)
    return target

