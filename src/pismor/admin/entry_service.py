"""管理后台条目校验：创建前校验原语、编辑前校验、键变更与关联数据同步。

调用方负责开启事务；本模块不 commit。条目四元组
(user_source,user_id,dimension,topic) 是主键，id 是派生的 mark，
编辑键时必须同步 sources / review_history / conflict_with / 澄清与向量。

校验原语（check_keys/check_taxonomy/check_content/check_safety/check_tombstone）
为创建与编辑两条路径共用；安全规则一律经 pipeline/safety.py（单一权威），
本模块不保留副本定义。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from pismor.core.keys import entry_key
from pismor.core.schemas import _KEY_UNSAFE_CHARS
from pismor.pipeline.safety import (
    has_sensitive_detail,
    normalize_topic,
    quote_is_negated,
    scan_instruction,
    sensitive_cap,
    topics_near,
)
from pismor.pipeline import writer


STATUSES = frozenset({
    "pending_review", "orphan_topic", "active", "degraded", "archived",
})

# 键字段长度上限（与 core/schemas.py 的 source/user_ref max_length 一致）
_KEY_MAX_LEN = 128


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class FieldError:
    """字段级校验错误：field 是表单字段名，code 是稳定的机器可读码。"""
    field: str
    code: str
    message: str


@dataclass
class CheckResult:
    """check_entry_create 的结果；ok 只反映字段级硬错误，conflicts 由路由判 409。"""
    ok: bool
    errors: list[FieldError]
    warnings: list[str]
    conflicts: list[dict]           # {id, dimension, topic, status, summary, ...}
    normalized_topic: str


def _entry_row(conn, *, user_source: str, user_id: str, entry_id: str):
    return conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=? AND id=?",
        (user_source, user_id, entry_id),
    ).fetchone()


def check_keys(user_source: str, user_id: str) -> list[FieldError]:
    """结构键校验：空 → missing、超 128 字 → too_long、含控制字符 → unsafe_chars。

    一个键可同时产出多条错误（如既含控制字符又超长）；键含 \\x00-\\x1f/\\x7f
    会串号 vec_key（用户串扰），必须硬拒。
    """
    errors: list[FieldError] = []
    for field, value in (("user_source", user_source), ("user_id", user_id)):
        text = value or ""
        if not text.strip():
            errors.append(FieldError(field, "missing", f"{field} 不能为空"))
            continue
        if len(text) > _KEY_MAX_LEN:
            errors.append(FieldError(field, "too_long", f"{field} 超过 128 字符"))
        if _KEY_UNSAFE_CHARS.search(text):
            errors.append(FieldError(field, "unsafe_chars", f"{field} 含控制字符"))
    return errors


def check_taxonomy(
    taxonomy, dimension: str, topic: str, *, allow_foreign: bool,
) -> tuple[str, list[FieldError], list[str]]:
    """维度与主题校验，返回 (归一后 topic, errors, warnings)。

    顺序固定：维度存在 → normalize_topic（别名归一并判词表，ValueError 即词表外）
    → disabled_topics → 前缀 ~+! → 长度 ≤128。allow_foreign=False（新建）时
    词表外/停用/前缀是硬错误；True（编辑）时降级为 warning——编辑只改存量
    条目，不能因词表演进堵死存量数据的修改路径。维度不存在始终是 error。
    """
    errors: list[FieldError] = []
    warnings: list[str] = []
    dimension = (dimension or "").strip()
    topic = (topic or "").strip()

    dim = taxonomy.dimensions.get(dimension)
    if dim is None:
        errors.append(FieldError("dimension", "missing", f"维度不存在: {dimension}"))
        return topic, errors, warnings

    try:
        normalized = normalize_topic(taxonomy, dimension, topic)
    except ValueError:
        normalized = topic
        if allow_foreign:
            warnings.append("主题不在当前词表中；本次只修改存量条目，不会新增主题")
        else:
            errors.append(FieldError("topic", "foreign", "主题不在词表中"))
    if normalized in (dim.disabled_topics or []):
        if allow_foreign:
            warnings.append("主题已停用；停用只阻止新条目，存量条目仍可编辑")
        else:
            errors.append(FieldError("topic", "disabled", "主题已停用，不能新建条目"))
    if normalized.startswith(("~", "+", "!")):
        message = "主题不能以 ~、+、! 开头"
        if allow_foreign:
            warnings.append(message)
        else:
            errors.append(FieldError("topic", "prefix", message))
    if len(normalized) > _KEY_MAX_LEN:
        message = "主题不能超过 128 字"
        if allow_foreign:
            warnings.append(message)
        else:
            errors.append(FieldError("topic", "too_long", message))
    return normalized, errors, warnings


def check_content(
    summary: str, body: str, quote: str | None, confidence: float,
) -> list[FieldError]:
    """内容长度与置信度校验：summary 1..20、body 1..200、quote 1..500、confidence 0..1。

    quote=None 跳过该字段（编辑路径没有 quote）；错误顺序与编辑路径的
    历史错误顺序一致（置信度→摘要→正文）。
    """
    errors: list[FieldError] = []
    if not 0 <= confidence <= 1:
        errors.append(FieldError("confidence", "out_of_range", "置信度必须在 0 到 1 之间"))
    summary = (summary or "").strip()
    if not summary:
        errors.append(FieldError("summary", "missing", "摘要不能为空"))
    elif len(summary) > 20:
        errors.append(FieldError("summary", "too_long", "摘要不能超过 20 字"))
    body = (body or "").strip()
    if not body:
        errors.append(FieldError("body", "missing", "正文不能为空"))
    elif len(body) > 200:
        errors.append(FieldError("body", "too_long", "正文不能超过 200 字"))
    if quote is not None:
        quote = (quote or "").strip()
        if not quote:
            errors.append(FieldError("quote", "missing", "原话引用不能为空"))
        elif len(quote) > 500:
            errors.append(FieldError("quote", "too_long", "原话引用不能超过 500 字"))
    return errors


def check_safety(
    taxonomy, dimension: str, topic: str,
    summary: str, body: str, quote: str,
) -> tuple[list[FieldError], list[str], float]:
    """安全规则校验，返回 (errors, warnings, cap)，cap = sensitive_cap(...) or 1.0。

    topic 须传归一后的 canonical（sensitive_cap 按 topic_overrides 的
    canonical key 查）。指令检测用拼接后的整体文本扫描：\\s* 可跨段吸收
    \\n，分段传参会漏掉跨边界命中。
    """
    errors: list[FieldError] = []
    warnings: list[str] = []

    if scan_instruction(f"{body}\n{quote}\n{summary}"):
        errors.append(FieldError(
            "body", "instruction_detected", "内容包含指令注入意图，拒绝保存"))
    if has_sensitive_detail(body, quote):
        errors.append(FieldError(
            "body", "sensitive_detail",
            "敏感主题不得包含精确细节（门牌/楼栋/确诊/处方），请改为概括性描述"))
    if dimension == "taboo" and not quote_is_negated(quote):
        errors.append(FieldError(
            "quote", "quote_not_negated", "禁忌条目的原话必须包含否定词"))

    cap = sensitive_cap(taxonomy, dimension, topic) or 1.0
    if cap < 1.0 and not any(e.code == "sensitive_detail" for e in errors):
        warnings.append(f"该主题为敏感主题：仅可保存概括性内容，置信度上限 {cap}")
    return errors, warnings, cap


def check_tombstone(conn, user_source: str, user_id: str) -> list[FieldError]:
    """用户墓碑检查（查询模式同 serving/audit.py）。"""
    if conn.execute(
        "SELECT 1 FROM tombstones WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchone():
        return [FieldError("user_id", "user_deleted", "用户已删除，恢复后才能新建条目")]
    return []


def _conflict_columns() -> str:
    return "id, dimension, topic, status, summary, confidence, updated_at"


def find_same_key(
    conn, user_source: str, user_id: str, dimension: str, topic: str,
) -> dict | None:
    """同键既有条目（不限状态：archived 也算，创建时提示走编辑恢复）；无 → None。"""
    row = conn.execute(
        f"SELECT {_conflict_columns()} FROM entries"
        " WHERE user_source=? AND user_id=? AND dimension=? AND topic=?",
        (user_source, user_id, dimension, topic),
    ).fetchone()
    return dict(row) if row else None


def find_near_taboo(
    conn, user_source: str, user_id: str, dimension: str, topic: str,
) -> dict | None:
    """近似 taboo 行：存活（active/degraded）、不带 ~ 标记、topics_near 匹配
    且 topic 不同；取第一条；无 → None。taboo 维度自身不查。"""
    if dimension == "taboo":
        return None
    for row in conn.execute(
        f"SELECT {_conflict_columns()} FROM entries"
        " WHERE user_source=? AND user_id=? AND dimension='taboo'"
        " AND status IN ('active','degraded') AND topic NOT LIKE '~%'",
        (user_source, user_id),
    ):
        if row["topic"] != topic and topics_near(topic, row["topic"]):
            return dict(row)
    return None


def check_entry_create(
    conn, taxonomy, *, user_source, user_id, dimension, topic,
    summary, body, quote, confidence,
) -> CheckResult:
    """创建前校验（纯本地：无 LLM、不查近似 taboo/近邻——那些在 create_entry 做）。

    组装顺序即 Global Constraints：键 → 词表(硬拒) → 内容 → 安全
    → tombstone → 同键；归一后 topic 用于同键判定并回传给调用方。
    """
    errors: list[FieldError] = []
    warnings: list[str] = []
    dimension = (dimension or "").strip()
    topic = (topic or "").strip()
    summary = (summary or "").strip()
    body = (body or "").strip()
    quote = (quote or "").strip()

    errors.extend(check_keys(user_source, user_id))
    normalized_topic, topic_errors, topic_warnings = check_taxonomy(
        taxonomy, dimension, topic, allow_foreign=False,
    )
    errors.extend(topic_errors)
    warnings.extend(topic_warnings)
    errors.extend(check_content(summary, body, quote, confidence))
    safety_errors, safety_warnings, _cap = check_safety(
        taxonomy, dimension, normalized_topic, summary, body, quote,
    )
    errors.extend(safety_errors)
    warnings.extend(safety_warnings)
    errors.extend(check_tombstone(conn, user_source, user_id))
    conflict = find_same_key(conn, user_source, user_id, dimension, normalized_topic)
    conflicts = [conflict] if conflict else []

    return CheckResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        conflicts=conflicts,
        normalized_topic=normalized_topic,
    )


def check_entry_edit(
    conn,
    taxonomy,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
    new_dimension: str,
    new_topic: str,
    confidence: float,
    status: str,
    summary: str,
    body: str,
    degraded_reason: str | None = None,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    conflicts: list[dict] = []

    row = _entry_row(
        conn, user_source=user_source, user_id=user_id,
        entry_id=entry_id,
    )
    if row is None:
        return {
            "ok": False,
            "errors": ["条目不存在或已被删除"],
            "warnings": [],
            "conflicts": [],
        }
    if conn.execute(
        "SELECT 1 FROM tombstones WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchone():
        return {
            "ok": False,
            "errors": ["用户已删除，恢复后才能编辑条目"],
            "warnings": [],
            "conflicts": [],
        }

    new_dimension = (new_dimension or "").strip()
    new_topic = (new_topic or "").strip()
    summary = (summary or "").strip()
    body = (body or "").strip()

    if status not in STATUSES:
        errors.append(f"未知状态: {status}")
    # 组装校验原语；编辑无 quote（None 跳过），词表外/停用仅警告（编辑兼容），
    # FieldError 的 message 即面向用户的既有文案
    errors.extend(fe.message for fe in check_content(summary, body, None, confidence))
    _, topic_errors, topic_warnings = check_taxonomy(
        taxonomy, new_dimension, new_topic, allow_foreign=True,
    )
    errors.extend(fe.message for fe in topic_errors)
    warnings.extend(topic_warnings)
    if status == "degraded" and not (degraded_reason or "").strip():
        warnings.append("状态为降级但未填写降级原因")

    if not errors:
        rows = conn.execute(
            "SELECT id, dimension, topic, status, summary FROM entries"
            " WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
            " AND id<>?",
            (
                user_source, user_id, new_dimension, new_topic, entry_id,
            ),
        ).fetchall()
        conflicts = [dict(item) for item in rows]

    return {
        "ok": not errors and not conflicts,
        "errors": errors,
        "warnings": warnings,
        "conflicts": conflicts,
    }


def _replace_marks(value: str | None, old_id: str, new_id: str) -> str | None:
    if not value:
        return value
    try:
        items = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
    if not isinstance(items, list):
        return value
    changed = False
    out: list = []
    for item in items:
        text = str(item)
        if text == old_id:
            out.append(new_id)
            changed = True
        else:
            out.append(item)
    return json.dumps(sorted(set(out)), ensure_ascii=False) if changed else value


def _rename_references(
    conn,
    *,
    user_source: str,
    user_id: str,
    old_id: str,
    new_id: str,
) -> None:
    conn.execute(
        "UPDATE sources SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (new_id, user_source, user_id, old_id),
    )
    conn.execute(
        "UPDATE review_history SET entry_id=? WHERE user_source=? AND user_id=? AND entry_id=?",
        (new_id, user_source, user_id, old_id),
    )
    conn.execute(
        "UPDATE entries SET superseded_by=? WHERE user_source=? AND user_id=?"
        " AND superseded_by=?",
        (new_id, user_source, user_id, old_id),
    )

    for item in conn.execute(
        "SELECT id, conflict_with FROM entries"
        " WHERE user_source=? AND user_id=? AND conflict_with IS NOT NULL",
        (user_source, user_id),
    ).fetchall():
        replaced = _replace_marks(item["conflict_with"], old_id, new_id)
        if replaced != item["conflict_with"]:
            conn.execute(
                "UPDATE entries SET conflict_with=? WHERE user_source=? AND user_id=?"
                " AND id=?",
                (replaced, user_source, user_id, item["id"]),
            )

    for item in conn.execute(
        "SELECT user_source, user_id, conflicting_ids FROM pending_clarifications"
        " WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchall():
        replaced = _replace_marks(item["conflicting_ids"], old_id, new_id)
        if replaced == item["conflicting_ids"]:
            continue
        try:
            conn.execute(
                "UPDATE pending_clarifications SET conflicting_ids=?"
                " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                (replaced, item["user_source"], item["user_id"], item["conflicting_ids"]),
            )
        except sqlite3.IntegrityError:
            # 新标记组合已有待确认问题：旧行已被覆盖，删除重复行即可。
            conn.execute(
                "DELETE FROM pending_clarifications"
                " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                (item["user_source"], item["user_id"], item["conflicting_ids"]),
            )


def apply_entry_edit(
    conn,
    taxonomy,
    *,
    user_source: str,
    user_id: str,
    entry_id: str,
    expected_version: str,
    new_dimension: str,
    new_topic: str,
    confidence: float,
    status: str,
    summary: str,
    body: str,
    degraded_reason: str | None = None,
    vector: list[float] | None = None,
    vector_requested: bool = False,
) -> dict:
    checked = check_entry_edit(
        conn, taxonomy,
        user_source=user_source, user_id=user_id, entry_id=entry_id,
        new_dimension=new_dimension, new_topic=new_topic,
        confidence=confidence, status=status, summary=summary, body=body,
        degraded_reason=degraded_reason,
    )
    if not checked["ok"]:
        return {**checked, "stale": False, "id": None, "updated_at": None}

    row = _entry_row(
        conn, user_source=user_source, user_id=user_id,
        entry_id=entry_id,
    )
    if row is None:
        return {
            "ok": False, "errors": ["条目不存在或已被删除"], "warnings": [],
            "conflicts": [], "stale": False, "id": None, "updated_at": None,
        }

    new_dimension = (new_dimension or "").strip()
    new_topic = (new_topic or "").strip()
    new_id = entry_key(new_dimension, new_topic)
    now = utcnow()
    archived_at = row["archived_at"]
    if status == "archived":
        archived_at = archived_at or now
    else:
        archived_at = None
    superseded_by = row["superseded_by"] if status == "archived" else None

    current = conn.execute(
        "UPDATE entries SET dimension=?, topic=?, id=?, confidence=?,"
        " status=?, body=?, summary=?, degraded_reason=?, archived_at=?,"
        " superseded_by=?, topic_new=NULL, updated_at=?"
        " WHERE user_source=? AND user_id=? AND id=? AND updated_at=?",
        (
            new_dimension, new_topic, new_id, confidence, status,
            body.strip(), summary.strip(), (degraded_reason or "").strip() or None,
            archived_at, superseded_by, now,
            user_source, user_id, entry_id, expected_version,
        ),
    )
    if current.rowcount == 0:
        return {
            "ok": False, "errors": ["条目已变化，请刷新后重试"], "warnings": [],
            "conflicts": [], "stale": True, "id": None, "updated_at": None,
        }

    if new_id != entry_id:
        _rename_references(
            conn,
            user_source=user_source, user_id=user_id,
            old_id=entry_id, new_id=new_id,
        )

    vector_state = writer.sync_entry_vector(
        conn,
        user_source=user_source, user_id=user_id,
        old_dimension=row["dimension"], old_topic=row["topic"],
        new_dimension=new_dimension, new_topic=new_topic,
        old_body=row["body"], new_body=body.strip(),
        vector=vector, vector_requested=vector_requested,
    )
    warnings = list(checked["warnings"])
    if vector_state == "replaced":
        warnings.append("正文已更新，向量已重建")
    elif vector_state == "moved":
        warnings.append("向量已随条目键迁移")
    elif vector_state == "dropped":
        if vector_requested:
            warnings.append("正文已更新，嵌入服务不可用，该条目已退出向量召回")
        else:
            warnings.append("正文已更新，嵌入未启用，旧向量已移除")

    return {
        "ok": True,
        "errors": [],
        "warnings": warnings,
        "conflicts": [],
        "stale": False,
        "id": new_id,
        "updated_at": now,
    }
