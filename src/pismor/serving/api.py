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

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
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
from pismor.core.keys import valid_scope
from pismor.serving.merge_task import DbMergeTaskSink
from pismor.serving.review import apply_review_decision
from pismor.core.db import get_conn, init_schema
from pismor.core.stores import SettingsStore, TaxonomyStore
from pismor.llm.embedder import Embedder
from pismor.llm.client import LLMClient
from pismor.pipeline.runner import run_pipeline
from pismor.pipeline import writer
from pismor.core.taxonomy import load_taxonomy
from pismor.core.taxonomy_edit import write_pending_alias

logger = logging.getLogger("mem.api")


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

    @app.post("/events")
    def post_events(event: dict, background: BackgroundTasks):
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            if is_tombstoned(conn, str(event.get("source") or ""),
                             str(event.get("user_ref") or "")):
                return JSONResponse({"detail": "memory deleted"}, status_code=410)
            try:
                ok, ack = intake.intake_event(
                    conn, event, max_content_chars=s.intake.max_content_chars)
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
                    query: str | None = None, scope: str | None = None):
        dims = ([d.strip() for d in dimensions.split(",") if d.strip()]
                if dimensions else None)
        if scope is not None and not valid_scope(scope):
            raise HTTPException(status_code=422, detail="invalid scope")
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            # 降级链（主查询→必带集→空包）在 assembler 内，永不 5xx；tombstone → 空包
            a = s.assembler
            return assembler.assemble(conn, embedder, source, native_id, budget=budget,
                                      dimensions=dims, include_memory=include_memory,
                                      query=query, current_scope=scope,
                                      audit_sink=DbMergeTaskSink(s.db_path, source,
                                                                 native_id, scope),
                                      dimension_order=taxonomy_store.current().priority_order(),
                                      taboo_budget_share=a.taboo_budget_share,
                                      taboo_merge_threshold=a.taboo_merge_threshold,
                                      related_top_k=a.related_top_k,
                                      half_life_days=a.half_life_days,
                                      clarify_ttl_days=a.clarify_ttl_days,
                                      min_similarity=s.embedding.min_similarity)

    @app.get("/review/queue")
    def review_queue():
        # 复核队列 = pending_review 行 + 未消解的 '+' 变形矛盾双存行
        #（~ 轨迹行恒 archived 不命中；conflict_with 已清空 = 矛盾已消解，移出队列）
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            rows = conn.execute(
                "SELECT id AS entry_id, user_source, user_id, dimension, topic, scope, summary,"
                " confidence, status, updated_at AS entry_version, degraded_reason AS note"
                " FROM entries WHERE NOT EXISTS (SELECT 1 FROM tombstones t"
                "   WHERE t.user_source=entries.user_source AND t.user_id=entries.user_id)"
                " AND (status='pending_review'"
                "    OR (topic LIKE '+%' AND status IN ('active','degraded')"
                "        AND conflict_with IS NOT NULL))"
                " ORDER BY updated_at").fetchall()
        return {"items": [dict(r) for r in rows]}

    class DecisionIn(BaseModel):
        decision: Literal["confirm", "remove", "fix"]
        entry_version: str  # 竞态标记 = updated_at（spec §14）
        note: str | None = None
        body: str | None = None  # fix 判定的覆盖内容
        summary: str | None = None
        topic: str | None = None  # confirm 时归一目标 topic（必须在维度白名单内）

    @app.post("/review/{user_source}/{user_id}/{scope}/{entry_id}/decision")
    def review_decision(user_source: str, user_id: str, scope: str, entry_id: str, payload: DecisionIn):
        s = settings_store.current()
        with closing(get_conn(s.db_path)) as conn:
            if is_tombstoned(conn, user_source, user_id):
                return JSONResponse({"detail": "memory deleted"}, status_code=410)
            # 向量在写事务外预计算（网络 IO 不持有 WAL 写锁）
            vector = None
            if payload.decision == "fix" and payload.body is not None and embedder is not None:
                current = conn.execute(
                    "SELECT body FROM entries WHERE user_source=? AND user_id=? AND id=? AND scope=?",
                    (user_source, user_id, entry_id, scope),
                ).fetchone()
                if current is not None and payload.body.strip() != current["body"]:
                    vector = writer.compute_vector(embedder, payload.body)
            with conn:  # 判定+版本推进+矛盾收尾同事务：先到者生效、后到者版本失配 noop
                result = apply_review_decision(
                    conn,
                    user_source=user_source,
                    user_id=user_id,
                    scope=scope,
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
