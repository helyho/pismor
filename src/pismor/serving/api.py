"""FastAPI 装配：全路由 + 依赖注入（spec §16/§14/§20）。

- create_app(settings, llm=None, embedder=None)：默认按 settings 构造真实客户端，测试注入 fake。
- 连接隔离（I-1）：每请求/每后台任务独立 get_conn 短连接，用完即关——事务边界归各线程
  所有，杜绝共享连接被他人 commit/rollback 打穿（§20 条目+证据+向量同事务）。SQLite
  开连接成本可忽略；跨连接并发由 WAL + busy_timeout（get_conn 内置）承担。
- 幂等：intake 先登记后入队（登记与处理分离保证 202 语义）；重复事件 200 deduplicated。
- 畸形事件：intake 抛 pydantic.ValidationError → 全局 handler 转 422（Review Focus #5）。
- GET /memory 永不 5xx：降级链在 assembler 内部（主查询→必带集→空包）。
"""
import logging
from contextlib import closing
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from pismor.admin.deps import LOCAL_ADMIN
from pismor.admin.router import router as admin_router
from pismor.pipeline import intake
from pismor.serving import assembler
from pismor.serving.memory import is_tombstoned, soft_delete_memory
from pismor.serving.static import mount_admin
from pismor.core import tokenizer
from pismor.core.config import Settings
from pismor.serving.merge_task import DbMergeTaskSink
from pismor.serving.review import apply_review_decision
from pismor.core.db import get_conn, init_schema
from pismor.core.lifecycle import queue_condition
from pismor.core.stores import SettingsStore, TaxonomyStore
from pismor.llm.embedder import Embedder
from pismor.llm.client import LLMClient
from pismor.pipeline.runner import run_pipeline
from pismor.pipeline import writer
from pismor.core.taxonomy import load_taxonomy
from pismor.core.taxonomy_edit import write_pending_alias

logger = logging.getLogger("mem.api")


class _BodyTooLarge(Exception):
    pass


async def _reject_too_large(send) -> None:
    body = b'{"detail":"request body too large"}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class _BodyLimitMiddleware:
    """按 Content-Length 与累计实收字节拦截超大请求（uvicorn 本身无 body 上限）。"""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers") or []:
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = self.max_bytes + 1
                if declared > self.max_bytes:
                    await _reject_too_large(send)
                    return
        received = 0
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            # 应用尚未发响应头时才回 413，避免二次 http.response.start
            if not started:
                await _reject_too_large(send)


def _pipeline_task(settings_store: SettingsStore, taxonomy_store: TaxonomyStore,
                   event: dict, llm, embedder) -> None:
    settings = settings_store.current()
    conn = get_conn(settings.db_path)  # 后台任务独立短连接（I-1）
    try:
        run_pipeline(conn, settings, event, taxonomy=taxonomy_store.current(),
                     llm=llm, embedder=embedder)
    finally:
        conn.close()


def create_app(settings: Settings, llm=None, embedder=None) -> FastAPI:
    tokenizer.set_tokenizer(settings.assembler.tokenizer)  # mem_fts 分词器同源（先于 init_schema）
    taxonomy = load_taxonomy(settings.taxonomy_path)
    settings_store = SettingsStore(settings.config_path, settings)
    taxonomy_store = TaxonomyStore(settings.taxonomy_path, taxonomy)
    llm = llm or LLMClient(settings.llm)
    if embedder is None:
        embedder = Embedder(settings.embedding) if settings.embedding.enabled else None
    # schema/vec 表一次性确保存在（独立短连接，建完即关）
    vec_dimensions = settings.embedding.dimensions if settings.embedding.enabled else None
    with closing(get_conn(settings.db_path)) as conn:
        init_schema(conn, vec_dimensions)

    app = FastAPI(title="mem")
    app.add_middleware(_BodyLimitMiddleware,
                       max_bytes=settings.serve.max_request_bytes)
    app.state.settings_store = settings_store
    app.state.taxonomy_store = taxonomy_store
    app.state.settings = settings
    app.state.taxonomy = taxonomy
    app.state.llm = llm
    app.state.embedder = embedder
    app.state.principal = LOCAL_ADMIN

    @app.exception_handler(ValidationError)
    async def _validation_error(request: Request, exc: ValidationError):
        # 事件 schema 校验失败（intake 校验先于落库）→ 422，不进管线
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(intake.ContentTooLong)
    async def _content_too_long(request: Request, exc: intake.ContentTooLong):
        # conversation 事件的条数/单条/总量超限：先于 events_seen 落库 → 422
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.post("/events")
    def post_events(event: dict, background: BackgroundTasks):
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            if is_tombstoned(conn, str(event.get("source") or ""),
                             str(event.get("user_ref") or "")):
                return JSONResponse({"detail": "memory deleted"}, status_code=410)
            try:
                ok, ack = intake.intake_event(
                    conn, event,
                    max_content_chars=s.intake.max_content_chars,
                    max_messages=s.intake.max_messages,
                    max_message_chars=s.intake.max_message_chars,
                    max_total_chars=s.intake.max_total_chars)
            except ValidationError:
                raise  # 畸形事件：全局 handler 转 422（含 content 超限）
        if not ok:
            return JSONResponse(ack, status_code=200)  # {"deduplicated": true}
        background.add_task(_pipeline_task, settings_store, taxonomy_store,
                            event, llm, embedder)
        return JSONResponse({"event_id": ack["event_id"]}, status_code=202)

    @app.get("/memory/{source}/{native_id}")
    def get_memory(source: str, native_id: str, budget: int = 2000,
                    dimensions: str | None = None, include_memory: bool = True,
                    query: str | None = None):
        dims = ([d.strip() for d in dimensions.split(",") if d.strip()]
                if dimensions else None)
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            # 降级链（主查询→必带集→空包）在 assembler 内，永不 5xx；tombstone → 空包
            a = s.assembler
            return assembler.assemble(conn, embedder, source, native_id, budget=budget,
                                      dimensions=dims, include_memory=include_memory,
                                      query=query,
                                      audit_sink=DbMergeTaskSink(s.db_path, source,
                                                                 native_id),
                                      dimension_order=taxonomy_store.current().priority_order(),
                                      taboo_budget_share=a.taboo_budget_share,
                                      taboo_merge_threshold=a.taboo_merge_threshold,
                                      related_top_k=a.related_top_k,
                                      half_life_days=a.half_life_days,
                                      clarify_ttl_days=a.clarify_ttl_days,
                                      recall_conflicts=a.recall_conflicts,
                                      min_similarity=s.embedding.min_similarity)

    @app.get("/review/queue")
    def review_queue():
        # 复核队列 = pending_review 行 + 未消解的 conflict 形态矛盾双存行
        #（trace 行恒 archived 不命中；conflict_with 已清空 = 矛盾已消解，移出队列）
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            rows = conn.execute(
                "SELECT id AS entry_id, user_source, user_id, dimension, topic, summary,"
                " confidence, status, updated_at AS entry_version, degraded_reason AS note"
                f" FROM entries WHERE {queue_condition()}"
                " ORDER BY updated_at").fetchall()
        return {"items": [dict(r) for r in rows]}

    class DecisionIn(BaseModel):
        decision: Literal["confirm", "remove", "fix"]
        entry_version: str  # 竞态标记 = updated_at（spec §14）
        note: str | None = None
        body: str | None = None  # fix 判定的覆盖内容
        summary: str | None = None
        topic: str | None = None  # confirm 时归一目标 topic（必须在维度白名单内）

    @app.post("/review/{user_source}/{user_id}/{entry_id}/decision")
    def review_decision(user_source: str, user_id: str, entry_id: str, payload: DecisionIn):
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            if is_tombstoned(conn, user_source, user_id):
                return JSONResponse({"detail": "memory deleted"}, status_code=410)
            # 向量在写事务外预计算（网络 IO 不持有 WAL 写锁）
            vector = None
            if payload.decision == "fix" and payload.body is not None and embedder is not None:
                current = conn.execute(
                    "SELECT body FROM entries WHERE user_source=? AND user_id=? AND id=?",
                    (user_source, user_id, entry_id),
                ).fetchone()
                if current is not None and payload.body.strip() != current["body"]:
                    vector = writer.compute_vector(embedder, payload.body)
            with conn:  # 判定+版本推进+矛盾收尾同事务：先到者生效、后到者版本失配 noop
                result = apply_review_decision(
                    conn,
                    user_source=user_source,
                    user_id=user_id,
                    entry_id=entry_id,
                    decision=payload.decision,
                    entry_version=payload.entry_version,
                    note=payload.note,
                    body=payload.body,
                    summary=payload.summary,
                    topic=payload.topic,
                    taxonomy=taxonomy_store.current(),
                    vector=vector,
                    vector_requested=embedder is not None,
                    taboo_merge_threshold=s.assembler.taboo_merge_threshold,
                )
            # 别名落盘在事务提交之后：文件写失败不影响已提交的复核结果
            pending = result.pop("_pending_alias", None) if isinstance(result, dict) else None
            if pending:
                try:
                    new = write_pending_alias(
                        taxonomy_store.current(), pending,
                        backup_root=s.backup.root / "config", keep=s.backup.keep)
                    if new is not None:
                        taxonomy_store.replace(new)
                except Exception:
                    logger.warning("topic alias write failed: %s", pending, exc_info=True)
        return result

    @app.delete("/memory/{source}/{native_id}")
    def delete_memory(source: str, native_id: str, request: Request):
        requested_by = request.headers.get("x-requested-by", "api")
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            with conn:  # 默认软删除：只写 tombstones，条目保留，可通过管理后台恢复
                soft_delete_memory(conn, user_source=source, user_id=native_id,
                                    requested_by=requested_by)
        return {"deleted": True, "mode": "soft"}

    @app.delete("/memory/{source}/{native_id}/entries/{entry_id}")
    def delete_memory_entry(source: str, native_id: str, entry_id: str, request: Request):
        from fastapi import Response

        from pismor.admin.audit import record_actor_audit
        from pismor.core.keys import display_key
        from pismor.serving.purge import purge_entry

        requested_by = request.headers.get("x-requested-by", "api")
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            if is_tombstoned(conn, source, native_id):
                return JSONResponse({"detail": "memory deleted"}, status_code=410)
            target = conn.execute(
                "SELECT dimension, topic FROM entries"
                " WHERE user_source=? AND user_id=? AND id=?",
                (source, native_id, entry_id),
            ).fetchone()
            if target is None:
                return Response(status_code=204)
            with conn:
                deleted = purge_entry(
                    conn,
                    user_source=source,
                    user_id=native_id,
                    entry_id=entry_id,
                    cascade_conflicts=True,
                    taboo_merge_threshold=s.assembler.taboo_merge_threshold,
                )
                record_actor_audit(
                    conn,
                    requested_by,
                    "entry.delete.public",
                    display_key(target["dimension"], target["topic"]),
                    {
                        "user_source": source,
                        "user_id": native_id,
                        "entry_id": entry_id,
                        "deleted_ids": deleted,
                    },
                )
        return {"deleted": deleted}

    @app.get("/healthz")
    def healthz():
        from sqlite3 import Error as SQLiteError
        try:
            s = settings_store.current()
            with closing(get_conn(s.db_path)) as conn:
                conn.execute("SELECT 1").fetchone()
        except SQLiteError:
            return JSONResponse({"ok": False, "db": "error"}, status_code=503)
        return {"ok": True, "db": "ok"}

    app.include_router(admin_router)
    mount_admin(app)
    return app
