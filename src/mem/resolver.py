"""四路决策器：ADD / UPDATE / CONFLICT / NOOP，另对 orphan 候选 ORPHAN 透传。

分流规则（spec §10/§11）：
- orphan 候选不调 LLM，直接 ORPHAN 透传（决策 LLM 只判 active 候选）；
- 库中无同 (user, dimension, topic, scope) 条目 → 直接 ADD，不进 LLM；
- 其余交决策 LLM，decision 枚举 UPDATE|CONFLICT|NOOP（RESOLVE_SCHEMA 约束）；
- taboo 恒胜特则：preference 候选与同主题 taboo 条目互斥时，无论 LLM 判什么
  都强制 CONFLICT（代码级硬约束，双保险于 prompt 特则句），taboo 侧按正常演进 UPDATE。
"""
from mem.prompts import RESOLVE_SYSTEM, RESOLVE_USER

# decision 枚举无 ADD：无新证据的 ADD 已在 existing 判断分流，不进入 LLM
RESOLVE_SCHEMA = {
    "name": "resolve_decision",
    "schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["UPDATE", "CONFLICT", "NOOP"]},
            "target": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["decision", "reason"],
    },
}


def resolve(conn, user_source, user_id, candidates, llm):
    actions = []
    for c in candidates:
        if c.get("status") == "orphan_topic":
            actions.append({"op": "ORPHAN", "candidate": c})
            continue
        existing = conn.execute(
            "SELECT * FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=? AND scope=?",
            (user_source, user_id, c["dimension"], c["topic"], c["scope"])).fetchone()
        if existing is None:
            actions.append({"op": "ADD", "candidate": c})
            continue
        taboo_row = _same_topic_taboo(conn, user_source, user_id, c)
        verdict = llm.chat_json(RESOLVE_SYSTEM, RESOLVE_USER.format(
            old_dim=existing["dimension"], topic=c["topic"], scope=c["scope"],
            old=existing["body"], new_dim=c["dimension"], new=c["body"],
            taboo=_taboo_note(taboo_row)), RESOLVE_SCHEMA)
        op = verdict.get("decision")
        if c["dimension"] == "preference" and taboo_row:
            op = "CONFLICT"  # taboo 恒胜：preference 侧降级，覆盖 LLM 判定
        target = (existing["dimension"], existing["topic"], existing["scope"])
        if op == "UPDATE":
            actions.append({"op": "UPDATE", "candidate": c, "target": target})
        elif op == "CONFLICT":
            actions.append({"op": "CONFLICT", "candidate": c, "target": target})
        else:
            actions.append({"op": "NOOP", "candidate": c, "target": target})
    return actions


def _same_topic_taboo(conn, user_source, user_id, c):
    """同 (user, topic, scope) 下的并存 taboo 条目；taboo 候选自身不构成互斥。"""
    if c["dimension"] == "taboo":
        return None
    return conn.execute(
        "SELECT id, body FROM entries WHERE user_source=? AND user_id=?"
        " AND dimension='taboo' AND topic=? AND scope=?",
        (user_source, user_id, c["topic"], c["scope"])).fetchone()


def _taboo_note(row):
    if row is None:
        return "无"
    return f"有（{row['id']}：{row['body']}）——taboo 恒胜，与 taboo 互斥的 preference 判定必须为 CONFLICT"
