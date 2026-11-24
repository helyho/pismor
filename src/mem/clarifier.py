"""澄清器：矛盾条目发出澄清问题（issue）、用户回话关联判定（match_incoming）、TTL 过期清理（expire）。

pending_clarifications 一行 = 一个待澄清矛盾对；TTL 默认 7 天（行级 ttl_days），
未过期判定与过期删除均按行级 ttl_days 用 SQLite julianday 差值计算，issued_at 存 UTC ISO。
"""
import json
from datetime import datetime, timezone

from mem.prompts import CLARIFY_CHECK_SYSTEM, CLARIFY_CHECK_USER

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


def issue(conn, user_source, user_id, conflicting_ids, summaries) -> str:
    """按矛盾对拼 question 落 pending_clarifications，返回 question 文本。

    summaries 以条目 id 为键；取 conflicting_ids 前两个 id 对应摘要拼模板。
    幂等去重：同一用户已存在未过期、conflicting_ids 集合相同的 pending → 直接复用
    其 question，不重复 INSERT（避免秒级 PK 冲突与重复装配行累积）。
    """
    ids = list(conflicting_ids)
    wanted = set(ids)
    for row in conn.execute(
            "SELECT conflicting_ids, question FROM pending_clarifications"
            " WHERE user_source=? AND user_id=?"
            "   AND julianday('now') - julianday(issued_at) <= ttl_days"
            " ORDER BY issued_at DESC", (user_source, user_id)).fetchall():
        try:
            if set(json.loads(row["conflicting_ids"])) == wanted:
                return row["question"]
        except (TypeError, ValueError):
            continue  # 坏行不影响新问题发出
    question = QUESTION_TMPL.format(s_a=summaries[ids[0]], s_b=summaries[ids[1]])
    conn.execute(
        "INSERT INTO pending_clarifications"
        " (user_source, user_id, conflicting_ids, question, issued_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_source, user_id, json.dumps(ids), question, _now_iso()))
    conn.commit()
    return question


def match_incoming(conn, user_source, user_id, text, llm) -> dict | None:
    """存在未过期 pending 时交 LLM 二元判定该消息是否在回答。

    是 → 返回 {"conflicting_ids": [...], "answer": text} 并删除该 pending 行；
    否 / 无 pending → None（pending 保留，等后续消息再判）。
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
    conn.execute(
        "DELETE FROM pending_clarifications WHERE user_source=? AND user_id=? AND issued_at=?",
        (user_source, user_id, row["issued_at"]))
    conn.commit()
    return {"conflicting_ids": json.loads(row["conflicting_ids"]), "answer": text}


def expire(conn, now) -> int:
    """删除 issued_at 距 now 超过 ttl_days 的 pending 行，返回删除数。"""
    cur = conn.execute(
        "DELETE FROM pending_clarifications WHERE julianday(?) - julianday(issued_at) > ttl_days",
        (now,))
    conn.commit()
    return cur.rowcount
