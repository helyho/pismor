"""澄清器：矛盾条目发出澄清问题（issue）、用户回话关联判定（match_incoming）、
TTL 过期清理（expire）与成功后消费（consume）。

pending_clarifications 一行 = 一个待澄清矛盾对；conflicting_ids 存 "{id}@{scope}"
标记（旧裸 id 兼容），主键 (user_source,user_id,conflicting_ids) 使同秒多对互不冲突。
TTL 默认 7 天（行级 ttl_days），未过期判定与过期删除均按行级 ttl_days 用 SQLite
julianday 差值计算，issued_at 存 UTC ISO。
"""
import json
from datetime import datetime, timezone

from pismor.core.keys import canonical_ids
from pismor.llm.prompts import CLARIFY_CHECK_SYSTEM, CLARIFY_CHECK_USER

QUESTION_TMPL = '你之前提过"{s_a}"，也提过"{s_b}"——日常对话里以哪种为准？'

ANSWER_CHECK_SCHEMA = {
    "name": "clarification_answer_check",
    "schema": {
        "type": "object",
        "properties": {"is_answer": {"type": "boolean"}},
        "required": ["is_answer"],
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def issue(conn, user_source, user_id, conflicting_ids, summaries,
          ttl_days: int = 7, *, commit: bool = True) -> str:
    """单对澄清：委托 issue_many（保持既有调用签名与提交语义）。"""
    return issue_many(conn, user_source, user_id, [(list(conflicting_ids), summaries)],
                      ttl_days=ttl_days, commit=commit)[0]


def issue_many(conn, user_source, user_id, pairs, ttl_days: int = 7, *,
               commit: bool = True) -> list[str]:
    """批量落 pending_clarifications，返回与 pairs 等长的 question 列表。

    pairs: [(conflicting_ids, summaries)]。全部行在同一事务内写入——GET /profile 装配
    路径可能一次产出多对，逐对提交会放大 WAL 写锁竞争（审查 #7）；commit=False 供日批
    等外层事务复用，避免提前提交破坏单事务（审查 #19）。
    summaries 以条目关联标记（id@scope）为键；取 conflicting_ids 前两个标记对应摘要
    拼模板。幂等去重：同一用户已存在未过期、集合相同的 pending → 直接复用其 question；
    同对过期后重发走 UPSERT 刷新 issued_at 与 ttl_days（TTL 重新计时），不同对同秒各自成行。
    ttl_days 为新建行 TTL（settings.assembler.clarify_ttl_days 配置）。
    """
    questions: list[str] = []
    if commit:
        with conn:
            for ids, summaries in pairs:
                questions.append(_issue_one(conn, user_source, user_id, ids, summaries, ttl_days))
    else:
        for ids, summaries in pairs:
            questions.append(_issue_one(conn, user_source, user_id, ids, summaries, ttl_days))
    return questions


def _issue_one(conn, user_source, user_id, conflicting_ids, summaries, ttl_days) -> str:
    ids = list(conflicting_ids)
    key = canonical_ids(ids)
    for row in conn.execute(
            "SELECT conflicting_ids, question FROM pending_clarifications"
            " WHERE user_source=? AND user_id=?"
            "   AND julianday('now') - julianday(issued_at) <= ttl_days"
            " ORDER BY issued_at DESC", (user_source, user_id)).fetchall():
        try:
            if canonical_ids(json.loads(row["conflicting_ids"])) == key:
                return row["question"]
        except (TypeError, ValueError):
            continue  # 坏行不影响新问题发出
    question = QUESTION_TMPL.format(s_a=summaries[ids[0]], s_b=summaries[ids[1]])
    conn.execute(
        "INSERT INTO pending_clarifications"
        " (user_source, user_id, conflicting_ids, question, issued_at, ttl_days)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_source, user_id, conflicting_ids) DO UPDATE SET"
        " question=excluded.question, issued_at=excluded.issued_at,"
        " ttl_days=excluded.ttl_days",
        (user_source, user_id, key, question, _now_iso(), ttl_days))
    return question


def match_incoming(conn, user_source, user_id, text, llm) -> dict | None:
    """存在未过期 pending 时交 LLM 二元判定该消息是否在回答。

    是 → 返回 {"conflicting_ids": [...], "answer": text, "pending_key": ...}；
    判定为否 / 无 pending → None（pending 保留）。命中后不删 pending：删除延后到
    管线成功写入（consume），写入失败时问题仍在，用户不会被静默放弃（I-17）。
    """
    row = conn.execute(
        "SELECT issued_at, conflicting_ids, question FROM pending_clarifications"
        " WHERE user_source=? AND user_id=?"
        "   AND julianday('now') - julianday(issued_at) <= ttl_days"
        " ORDER BY issued_at DESC LIMIT 1",
        (user_source, user_id)).fetchone()
    if row is None:
        return None
    verdict = llm.chat_json(CLARIFY_CHECK_SYSTEM, CLARIFY_CHECK_USER.format(
        question=row["question"], message=text), ANSWER_CHECK_SCHEMA)
    if not verdict.get("is_answer"):
        return None
    return {"conflicting_ids": json.loads(row["conflicting_ids"]),
            "pending_key": row["conflicting_ids"], "answer": text}


def consume(conn, user_source, user_id, conflicting_ids, *, commit: bool = True) -> int:
    """管线在澄清回答成功写入后调用：删除该矛盾对的 pending 行，返回删除数。"""
    wanted = canonical_ids(conflicting_ids)
    removed = 0
    for row in conn.execute(
            "SELECT conflicting_ids FROM pending_clarifications"
            " WHERE user_source=? AND user_id=?", (user_source, user_id)).fetchall():
        try:
            if canonical_ids(json.loads(row["conflicting_ids"])) == wanted:
                conn.execute(
                    "DELETE FROM pending_clarifications"
                    " WHERE user_source=? AND user_id=? AND conflicting_ids=?",
                    (user_source, user_id, row["conflicting_ids"]))
                removed += 1
        except (TypeError, ValueError):
            continue
    if commit:
        conn.commit()
    return removed


def expire(conn, now, *, commit: bool = True) -> int:
    """删除 issued_at 距 now 超过 ttl_days 的 pending 行，返回删除数。

    commit=False 供日批外层事务复用（审查 #19）。
    """
    cur = conn.execute(
        "DELETE FROM pending_clarifications WHERE julianday(?) - julianday(issued_at) > ttl_days",
        (now,))
    if commit:
        conn.commit()
    return cur.rowcount
