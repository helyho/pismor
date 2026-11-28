"""复核判定的用例编排：向量预计算 → 事务内判定 → 提交后别名落盘。

公开 API 与管理端只负责协议、鉴权与审计；这三步的顺序契约在这里唯一实现。
"""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pismor.core.taxonomy_edit import write_pending_alias
from pismor.pipeline import writer
from pismor.serving.review import apply_review_decision

logger = logging.getLogger("mem.serving.review_flow")


@dataclass(frozen=True)
class DecisionRequest:
    """一次复核判定的请求值；两个入口的 schema 都能映射到它。"""

    user_source: str
    user_id: str
    entry_id: str
    decision: str
    entry_version: str
    note: str | None = None
    body: str | None = None
    summary: str | None = None
    topic: str | None = None


@dataclass(frozen=True)
class ReviewRuntime:
    """执行一次判定所需的运行时依赖；配置绑定集中在 from_settings。"""

    taxonomy: Any
    embedder: Any = None
    taboo_merge_threshold: int | None = None
    backup_root: Path | None = None
    backup_keep: int = 30
    taxonomy_store: Any = None
    in_txn: Callable[[Any, dict], None] | None = None

    @classmethod
    def from_settings(
        cls,
        settings,
        *,
        taxonomy,
        embedder=None,
        taxonomy_store=None,
        in_txn=None,
    ) -> "ReviewRuntime":
        return cls(
            taxonomy=taxonomy,
            embedder=embedder,
            taboo_merge_threshold=settings.assembler.taboo_merge_threshold,
            backup_root=settings.backup.root / "config",
            backup_keep=settings.backup.keep,
            taxonomy_store=taxonomy_store,
            in_txn=in_txn,
        )


def execute_decision(conn, request: DecisionRequest, runtime: ReviewRuntime) -> dict:
    """执行一次复核判定；事务边界与别名落盘顺序由本函数负责。"""
    vector = _precompute_vector(conn, request, runtime.embedder)
    with conn:  # 判定+版本推进+矛盾收尾同事务：先到者生效、后到者版本失配 noop
        result = apply_review_decision(
            conn,
            user_source=request.user_source,
            user_id=request.user_id,
            entry_id=request.entry_id,
            decision=request.decision,
            entry_version=request.entry_version,
            note=request.note,
            body=request.body,
            summary=request.summary,
            topic=request.topic,
            taxonomy=runtime.taxonomy,
            vector=vector,
            vector_requested=runtime.embedder is not None,
            taboo_merge_threshold=runtime.taboo_merge_threshold,
        )
        if runtime.in_txn is not None:
            runtime.in_txn(conn, result)

    pending = result.pop("_pending_alias", None) if isinstance(result, dict) else None
    if pending:
        _commit_alias(pending, runtime)
    return result


def _precompute_vector(conn, request: DecisionRequest, embedder):
    """fix 且正文变化时在写事务外算向量：网络 IO 不持有 WAL 写锁。"""
    if request.decision != "fix" or request.body is None or embedder is None:
        return None
    current = conn.execute(
        "SELECT body FROM entries WHERE user_source=? AND user_id=? AND id=?",
        (request.user_source, request.user_id, request.entry_id),
    ).fetchone()
    if current is None or request.body.strip() == current["body"]:
        return None
    return writer.compute_vector(embedder, request.body)


def _commit_alias(pending: dict, runtime: ReviewRuntime) -> None:
    """别名落盘在事务提交之后：文件写失败不影响已提交的复核结果。"""
    try:
        new = write_pending_alias(
            runtime.taxonomy,
            pending,
            backup_root=runtime.backup_root,
            keep=runtime.backup_keep,
        )
    except Exception:
        logger.warning("topic alias write failed: %s", pending, exc_info=True)
        return
    if new is not None and runtime.taxonomy_store is not None:
        runtime.taxonomy_store.replace(new)
