"""服务入口：启动 integrity_check + uvicorn + 每日聚合/审计/备份调度线程（KISS while-sleep）。"""
import argparse
import logging
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from mem.aggregator import run_daily
from mem.api import create_app
from mem.audit import isolate_violations, run_audit
from mem.backup import backup
from mem.config import load_settings
from mem.db import get_conn
from mem.llm import LLMClient
from mem.taxonomy import load_taxonomy

_RUN_INTERVAL_DEFAULT = 86400.0  # 默认日批间隔；settings.serve.daily_interval_seconds 可覆盖


def daily_once(conn, settings, taxonomy, llm, now, backup_dir) -> dict:
    """单轮日批：聚合（auto_promote 读 taxonomy.meta）→ 一致性审计/隔离 → 备份。"""
    stats = run_daily(conn, taxonomy, llm,
                      bool(taxonomy.meta.get("auto_promote", False)), now,
                      degrade_days=settings.aggregator.degrade_days,
                      degrade_confidence=settings.aggregator.degrade_confidence)
    violations = run_audit(conn, taxonomy)
    stats["audit_violations"] = len(violations)
    stats["isolated"] = isolate_violations(conn, violations, now)
    backup(conn, backup_dir, keep=settings.backup.keep)
    return stats


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    args = parser.parse_args(argv)

    settings = load_settings(args.settings)
    app = create_app(settings)
    with closing(get_conn(settings.db_path)) as conn:  # 独立短连接做启动检查
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if verdict != "ok":  # spec §20：库损坏时拒绝启动，走最近备份恢复
        raise SystemExit(f"integrity_check failed: {verdict}")

    threading.Thread(target=_daily_loop, args=(settings,), daemon=True).start()
    logging.basicConfig(level=logging.INFO)  # mem.* 模块 INFO 日志落 stderr（uvicorn 不配 root logger）
    uvicorn.run(app, host=args.host, port=args.port)


def _daily_loop(settings) -> None:
    log = logging.getLogger("mem.serve")
    taxonomy = load_taxonomy(settings.taxonomy_path)
    llm = LLMClient(settings.llm)
    backup_dir = settings.db_path.parent / "backups"
    while True:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            conn = get_conn(settings.db_path)  # 日批独立短连接（I-1），不与请求线程共享
            try:
                daily_once(conn, settings, taxonomy, llm, now, backup_dir)
            finally:
                conn.close()
        except Exception:
            log.exception("daily job failed")
        time.sleep(settings.serve.daily_interval_seconds)


if __name__ == "__main__":
    main()
