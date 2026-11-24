"""服务入口：启动 integrity_check + uvicorn + 每日聚合/备份调度线程（KISS while-sleep）。"""
import argparse
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from mem.aggregator import run_daily
from mem.api import create_app
from mem.backup import backup
from mem.config import load_settings
from mem.llm import LLMClient
from mem.taxonomy import load_taxonomy

_RUN_INTERVAL = 86400.0  # 每日一轮；启动即跑首轮（保底当日快照），此后 24h 循环


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    args = parser.parse_args(argv)

    settings = load_settings(args.settings)
    app = create_app(settings)
    conn = app.state.conn
    verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if verdict != "ok":  # spec §20：库损坏时拒绝启动，走最近备份恢复
        raise SystemExit(f"integrity_check failed: {verdict}")

    threading.Thread(target=_daily_loop, args=(conn, settings), daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port)


def _daily_loop(conn, settings) -> None:
    log = logging.getLogger("mem.serve")
    taxonomy = load_taxonomy(settings.taxonomy_path)
    llm = LLMClient(settings.llm)
    backup_dir = settings.db_path.parent / "backups"
    while True:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            # auto_promote=False：新主题一律进人工队列（保守默认，词表演进走复核）
            run_daily(conn, taxonomy, llm, False, now)
            backup(conn, backup_dir)
        except Exception:
            log.exception("daily job failed")
        time.sleep(_RUN_INTERVAL)


if __name__ == "__main__":
    main()
