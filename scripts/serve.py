"""服务入口：启动 integrity_check + uvicorn + 每日聚合/审计/备份调度线程（KISS while-sleep）。"""
import argparse
import json
import logging
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from pismor.pipeline.aggregator import run_daily
from pismor.serving.api import create_app
from pismor.core import tokenizer
from pismor.serving.audit import flag_violations, run_audit
from pismor.serving.backup import backup
from pismor.core.config import load_settings
from pismor.core.db import get_conn

_RUN_INTERVAL_DEFAULT = 86400.0  # 默认日批间隔；settings.serve.daily_interval_seconds 可覆盖


def _job_start(conn, job: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_runs (job, started_at) VALUES (?, ?)", (job, started_at)
    )
    conn.commit()
    return int(cur.lastrowid)


def _job_finish(conn, job_id: int, *, ok: bool, stats=None, error: str | None = None) -> None:
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats_json = json.dumps(stats, ensure_ascii=False, default=str) if stats is not None else None
    conn.execute(
        "UPDATE job_runs SET finished_at=?, ok=?, stats_json=?, error=? WHERE id=?",
        (finished_at, 1 if ok else 0, stats_json, (error or "")[:4000] or None, job_id),
    )
    conn.commit()


def daily_once(conn, settings, taxonomy, llm, now, backup_dir, *,
               taxonomy_backup_root=None, on_taxonomy_updated=None) -> dict:
    """单轮日批：聚合（auto_activate 读 taxonomy.meta）→ 一致性审计/标记 → 备份。"""
    job_id = _job_start(conn, "daily", now)
    try:
        stats = run_daily(conn, taxonomy, llm,
                          bool(taxonomy.meta.get("auto_activate", False)), now,
                          degrade_days=settings.aggregator.degrade_days,
                          degrade_confidence=settings.aggregator.degrade_confidence,
                          taxonomy_backup_root=taxonomy_backup_root,
                          taxonomy_backup_keep=settings.backup.keep,
                          taboo_merge_threshold=settings.assembler.taboo_merge_threshold,
                          on_taxonomy_updated=on_taxonomy_updated)
        violations = run_audit(conn, taxonomy, inferred_cap=settings.gate.inferred_cap)
        stats["audit_violations"] = len(violations)
        stats["flagged"] = flag_violations(conn, violations, now)
        backup(conn, backup_dir, keep=settings.backup.keep)
    except Exception as exc:
        _job_finish(conn, job_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    _job_finish(conn, job_id, ok=True, stats=stats)
    return stats


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    args = parser.parse_args(argv)

    settings = load_settings(args.settings)
    tokenizer.set_tokenizer(settings.assembler.tokenizer)  # mem_fts 分词器同源
    app = create_app(settings)
    with closing(get_conn(settings.db_path)) as conn:  # 独立短连接做启动检查
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":  # spec §20：库损坏时拒绝启动，走最近备份恢复
        raise SystemExit(f"integrity_check failed: {result}")

    threading.Thread(target=_daily_loop, args=(app,), daemon=True).start()
    logging.basicConfig(level=logging.INFO)  # mem.* 模块 INFO 日志落 stderr（uvicorn 不配 root logger）
    uvicorn.run(app, host=args.host, port=args.port)


def _daily_loop(app) -> None:
    log = logging.getLogger("mem.serve")
    while True:
        # 每轮取当前 store，配置中心保存后的字段/词表对下一轮日批生效。
        settings = app.state.settings_store.current()
        taxonomy = app.state.taxonomy_store.current()
        llm = app.state.llm
        backup_dir = settings.backup.root / "db"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            conn = get_conn(settings.db_path)  # 日批独立短连接（I-1），不与请求线程共享
            try:
                daily_once(
                    conn, settings, taxonomy, llm, now, backup_dir,
                    taxonomy_backup_root=settings.backup.root / "config",
                    on_taxonomy_updated=app.state.taxonomy_store.replace,
                )
            finally:
                conn.close()
        except Exception:
            log.exception("daily job failed")
        time.sleep(settings.serve.daily_interval_seconds)


if __name__ == "__main__":
    main()
