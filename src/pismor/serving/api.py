"""FastAPI 装配：全路由 + 依赖注入（spec §16/§14/§20）。

- create_app(settings, llm=None, embedder=None)：默认按 settings 构造真实客户端，测试注入 fake。
- 连接隔离（I-1）：每请求/每后台任务独立 get_conn 短连接，用完即关——事务边界归各线程
  所有，杜绝共享连接被他人 commit/rollback 打穿（§20 条目+证据+向量同事务）。SQLite
  开连接成本可忽略；跨连接并发由 WAL + busy_timeout（get_conn 内置）承担。
- 幂等：intake 先登记后入队（登记与处理分离保证 202 语义）；重复事件 200 deduplicated。
- 畸形事件：intake 抛 pydantic.ValidationError → 全局 handler 转 422（Review Focus #5）。
- GET /profile 永不 5xx：降级链在 assembler 内部（主查询→必带集→空包）。
"""
import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from pismor.pipeline import intake, writer
from pismor.serving import assembler, exporter
from pismor.core.config import Settings
from pismor.core.db import get_conn, init_schema
from pismor.llm.embedder import Embedder
from pismor.llm.client import LLMClient
from pismor.pipeline.runner import run_pipeline
from pismor.core.taxonomy import load_taxonomy

logger = logging.getLogger("mem.api")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pipeline_task(settings: Settings, event: dict, taxonomy, llm, embedder) -> None:
    conn = get_conn(settings.db_path)  # 后台任务独立短连接（I-1）
    try:
        run_pipeline(conn, settings, event, taxonomy=taxonomy, llm=llm, embedder=embedder)
    finally:
        conn.close()


def create_app(settings: Settings, llm=None, embedder=None) -> FastAPI:
    taxonomy = load_taxonomy(settings.taxonomy_path)
    llm = llm or LLMClient(settings.llm)
    if embedder is None:
        embedder = Embedder(settings.embedding) if settings.embedding.enabled else None
    # schema/vec 表一次性确保存在（独立短连接，建完即关）
    vec_dimensions = settings.embedding.dimensions if settings.embedding.enabled else None
    with closing(get_conn(settings.db_path)) as conn:
        init_schema(conn, vec_dimensions)

    app = FastAPI(title="mem")
    app.state.settings = settings
    app.state.taxonomy = taxonomy
    app.state.llm = llm
    app.state.embedder = embedder

    @app.exception_handler(ValidationError)
    async def _validation_error(request: Request, exc: ValidationError):
        # 事件 schema 校验失败（intake 校验先于落库）→ 422，不进管线
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.post("/events")
    def post_events(event: dict, background: BackgroundTasks):
        with closing(get_conn(settings.db_path)) as conn:
            try:
                ok, ack = intake.intake_event(
                    conn, event, max_content_chars=settings.intake.max_content_chars)
            except ValidationError:
                raise  # 畸形事件：全局 handler 转 422（含 content 超限）
        if not ok:
            return JSONResponse(ack, status_code=200)  # {"deduplicated": true}
        background.add_task(_pipeline_task, settings, event, taxonomy, llm, embedder)
        return JSONResponse({"event_id": ack["event_id"]}, status_code=202)

    @app.get("/profile/{source}/{native_id}")
    def get_profile(source: str, native_id: str, budget: int = 2000,
                    dimensions: str | None = None, include_profile: bool = True,
                    query: str | None = None):
        dims = ([d.strip() for d in dimensions.split(",") if d.strip()]
                if dimensions else None)
        with closing(get_conn(settings.db_path)) as conn:
            # 降级链（主查询→必带集→空包）在 assembler 内，永不 5xx；tombstone → 空包
            a = settings.assembler
            return assembler.assemble(conn, embedder, source, native_id, budget=budget,
                                      dimensions=dims, include_profile=include_profile,
                                      query=query,
                                      taboo_budget_share=a.taboo_budget_share,
                                      related_top_k=a.related_top_k,
                                      half_life_days=a.half_life_days,
                                      clarify_ttl_days=a.clarify_ttl_days)

    @app.get("/review/queue")
    def review_queue():
        # 复核队列 = pending_review 行 + 未消解的 '+' 变形矛盾双存行
        #（~ 轨迹行恒 archived 不命中；conflict_with 已清空 = 矛盾已消解，移出队列）
        with closing(get_conn(settings.db_path)) as conn:
            rows = conn.execute(
                "SELECT id AS entry_id, user_source, user_id, dimension, topic, scope, summary,"
                " confidence, status, updated_at AS entry_version, degraded_reason AS note"
                " FROM entries WHERE status='pending_review'"
                "    OR (topic LIKE '+%' AND status IN ('active','degraded')"
                "        AND conflict_with IS NOT NULL)"
                " ORDER BY updated_at").fetchall()
        return {"items": [dict(r) for r in rows]}

    class VerdictIn(BaseModel):
        verdict: Literal["approve", "discard", "fix"]
        entry_version: str  # 竞态标记 = updated_at（spec §14）
        note: str | None = None
        body: str | None = None  # fix 判定的覆盖内容
        summary: str | None = None

    @app.post("/review/{user_source}/{user_id}/{scope}/{entry_id}/verdict")
    def review_verdict(user_source: str, user_id: str, scope: str, entry_id: str, payload: VerdictIn):
        with closing(get_conn(settings.db_path)) as conn:
            with conn:  # 判定+版本推进+矛盾收尾同事务：先到者生效、后到者版本失配 noop
                row = conn.execute(
                    "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=? AND scope=?"
                    " AND updated_at=? AND topic NOT LIKE '~%'"
                    " AND status IN ('pending_review','active','degraded')",
                    (user_source, user_id, entry_id, scope, payload.entry_version)).fetchone()
                if row is None:
                    return {"noop": True}  # 版本已被先到者改写 → 后到者作废（spec §14）
                now = _utcnow()
                if payload.verdict == "discard":
                    # CAS：删除同样以 updated_at 为条件，行数 0 即判定作废
                    cur = conn.execute(
                        "DELETE FROM entries WHERE user_source=? AND user_id=? AND id=? AND scope=?"
                        " AND updated_at=?",
                        (user_source, user_id, entry_id, scope, payload.entry_version))
                    if cur.rowcount == 0:
                        return {"noop": True}
                    conn.execute(
                        "DELETE FROM sources WHERE user_source=? AND user_id=? AND entry_id=?",
                        (user_source, user_id, f"{row['id']}@{scope}"))
                else:
                    # orphan 候选 approve → 观察区（spec §8.2 晋升链入口），其余 → active
                    new_status = ("orphan_topic" if row["dimension"] == "orphan"
                                  else "active")
                    new_topic, new_id = row["topic"], row["id"]
                    fix_body = payload.body if payload.verdict == "fix" else None
                    fix_summary = payload.summary if payload.verdict == "fix" else None
                    if row["topic"].startswith("!"):
                        # 待复核快照 approve：规范键已被新证据占用 → 快照归档（不复活旧猜测）
                        canonical = row["topic"][1:]
                        occupied = conn.execute(
                            "SELECT 1 FROM entries WHERE user_source=? AND user_id=?"
                            " AND dimension=? AND topic=? AND scope=? AND id<>?",
                            (user_source, user_id, row["dimension"], canonical, scope,
                             row["id"])).fetchone()
                        if occupied:
                            cur = conn.execute(
                                "UPDATE entries SET status='archived', archived_at=?,"
                                " conflict_with=NULL, degraded_reason=NULL, updated_at=?"
                                " WHERE user_source=? AND user_id=? AND dimension=?"
                                " AND topic=? AND scope=? AND updated_at=?",
                                (now, now, user_source, user_id, row["dimension"],
                                 row["topic"], scope, payload.entry_version))
                            if cur.rowcount == 0:
                                return {"noop": True}
                            conn.execute(
                                "INSERT INTO review_history (entry_id, user_source, user_id,"
                                " verdict, actor, ts, note, entry_version)"
                                " VALUES (?, ?, ?, ?, 'reviewer', ?, ?, ?)",
                                (entry_id, user_source, user_id, payload.verdict, now,
                                 payload.note or "superseded_by_evolution",
                                 payload.entry_version))
                            return {"noop": False, "updated": 1}
                        new_topic, new_id = canonical, f"{row['dimension']}_{canonical}"
                    cur = conn.execute(
                        "UPDATE entries SET status=?, topic=?, id=?,"
                        " body=COALESCE(?, body), summary=COALESCE(?, summary),"
                        " degraded_reason=NULL, conflict_with=NULL, updated_at=?"
                        " WHERE user_source=? AND user_id=? AND dimension=?"
                        " AND topic=? AND scope=? AND updated_at=?",
                        (new_status, new_topic, new_id, fix_body, fix_summary, now,
                         user_source, user_id, row["dimension"], row["topic"], scope,
                         payload.entry_version))
                    if cur.rowcount == 0:  # 并发：先到者已改版本 → 本判定作废
                        return {"noop": True}
                    if new_id != row["id"]:  # 快照转正：证据链键跟随
                        conn.execute(
                            "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=?"
                            " AND entry_id=?",
                            (f"{new_id}@{scope}", user_source, user_id,
                             f"{row['id']}@{scope}"))
                    # 矛盾收尾（C-1）：按 (id,scope) 清对方行标记、conflict_lost 恢复 active
                    writer.resolve_conflicts(conn, user_source, user_id, entry_id, scope, now,
                                            revive=True, clear_pending=True)
                conn.execute(
                    "INSERT INTO review_history (entry_id, user_source, user_id, verdict,"
                    " actor, ts, note, entry_version) VALUES (?, ?, ?, ?, 'reviewer', ?, ?, ?)",
                    (entry_id, user_source, user_id, payload.verdict, now,
                     payload.note, payload.entry_version))
        return {"noop": False, "updated": 1}

    @app.delete("/profile/{source}/{native_id}")
    def delete_profile(source: str, native_id: str, request: Request):
        requested_by = request.headers.get("x-requested-by", "api")
        with closing(get_conn(settings.db_path)) as conn:
            with conn:  # 删除权（spec §16#4）：该用户全部画像行清除 + tombstones 留痕
                for table in ("entries", "sources", "review_history", "pending_clarifications"):
                    conn.execute(f"DELETE FROM {table} WHERE user_source=? AND user_id=?",
                                 (source, native_id))
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                                " AND name='embeddings'").fetchone():
                    conn.execute("DELETE FROM embeddings WHERE user_source=? AND user_id=?",
                                 (source, native_id))
                conn.execute(
                    "INSERT OR REPLACE INTO tombstones (user_source, user_id, deleted_at,"
                    " requested_by) VALUES (?, ?, ?, ?)",
                    (source, native_id, _utcnow(), requested_by))
        # 删除权覆盖导出副本：导出目录与库分离，但同为用户画像数据（spec §16#4）
        try:
            exporter.remove_export(settings.export_root, source, native_id)
        except (ValueError, OSError):
            pass  # 非法路径段/IO 失败：删除权以主库为准，不阻断删除响应
        return {"deleted": True}

    @app.get("/export/{source}/{native_id}")
    def export_profile(source: str, native_id: str):
        with closing(get_conn(settings.db_path)) as conn:
            try:
                root = exporter.export(conn, source, native_id, settings.export_root)
            except ValueError as exc:  # 路径穿越/非法路径段 → 400，不写任何文件
                return JSONResponse({"detail": str(exc)}, status_code=400)
        return {"root": str(root)}

    @app.get("/healthz")
    def healthz():
        from sqlite3 import Error as SQLiteError
        try:
            with closing(get_conn(settings.db_path)) as conn:
                conn.execute("SELECT 1").fetchone()
        except SQLiteError:
            return JSONResponse({"ok": False, "db": "error"}, status_code=503)
        return {"ok": True, "db": "ok"}

    return app
