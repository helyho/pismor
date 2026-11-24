"""SQLite 热备份：VACUUM INTO 每日快照 + 滚动保留 30 份（spec §20）。"""
import sqlite3
from datetime import datetime
from pathlib import Path

_KEEP = 30


def backup(conn: sqlite3.Connection, backup_dir) -> Path:
    """快照主库至 backup-YYYYMMDD.db（同日重跑覆盖），清理保留期外的旧快照。"""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"backup-{datetime.now():%Y%m%d}.db"
    if target.exists():
        target.unlink()  # VACUUM INTO 要求目标文件不存在
    conn.execute("VACUUM INTO ?", (str(target),))
    snapshots = sorted(backup_dir.glob("backup-*.db"))  # 文件名按日期，字典序即时间序
    for stale in snapshots[:-_KEEP]:
        stale.unlink(missing_ok=True)
    return target
