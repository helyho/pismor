"""四路决策器：ADD / UPDATE / CONFLICT / NOOP，另对 orphan 候选 ORPHAN 透传。

分流规则（spec §10/§11，第八轮扩展）：
- orphan 候选不调 LLM，直接 ORPHAN 透传（决策 LLM 只判 active 候选）；
- 库中无同 (user, dimension, topic, scope) 条目 → 近邻语义互斥检测（跨维度/跨 topic
  矛盾，第八轮新增）后 ADD，不进同 key 决策 LLM；
- 其余交决策 LLM，decision 枚举 UPDATE|CONFLICT|NOOP（RESOLVE_SCHEMA 约束）；
- taboo 恒胜特则：preference 候选与同主题 taboo 条目互斥时，无论 LLM 判什么
  都强制 CONFLICT（代码级硬约束，双保险于 prompt 特则句），taboo 侧按正常演进 UPDATE。

近邻互斥检测（spec §11 第八轮：矛盾判定从"同 key"扩展到"同 scope 语义互斥"）：
- 仅 taboo↔preference 配对（黑盒 A5 高发型：preference_style vs taboo_tone）；
- 仅同 scope（scope 拆分优先于矛盾判定）；对端行非同 key（同 key 已走决策器）；
- LLM 判互斥 → 走既有 CONFLICT 双存：'+' 变形行 + conflict_with 互标 {id}@{scope}，
  装配 clarification_needed 与复核队列按标记配对（机制零新增）。
"""
from pismor.core.keys import valid_scope
from pismor.llm.prompts import NEIGHBOR_SYSTEM, NEIGHBOR_USER, RESOLVE_SYSTEM, RESOLVE_USER

# decision 枚举无 ADD：无新证据的 ADD 已在 existing 判断分流，不进入 LLM
RESOLVE_SCHEMA = {
    "name": "resolve_decision",
    "schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["UPDATE", "CONFLICT", "NOOP"]},
            "reason": {"type": "string"},
        },
        "required": ["decision", "reason"],
    },
}

NEIGHBOR_SCHEMA = {
    "name": "neighbor_conflict",
    "schema": {
        "type": "object",
        "properties": {
            "conflict": {"type": "boolean"},
            "conflicting_with": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["conflict", "reason"],
    },
}

# 近邻比对维度配对（taboo↔preference 先行，控制成本与误判率）
_NEIGHBOR_PAIRS = {"taboo": ("preference",), "preference": ("taboo",)}
_NEIGHBOR_LIMIT = 5


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
            neighbor = _neighbor_conflict(conn, user_source, user_id, c, llm)
            if neighbor is not None:
                actions.append({"op": "CONFLICT", "candidate": c, "target": neighbor})
                continue
            actions.append({"op": "ADD", "candidate": c})
            continue
        if existing["status"] == "pending_review":
            # 待复核行不阻塞演进（I-2）：新证据直接演进，待复核快照由写入器移入复核队列
            actions.append({"op": "UPDATE", "candidate": c,
                            "target": (existing["dimension"], existing["topic"],
                                       existing["scope"])})
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


def _neighbor_conflict(conn, user_source, user_id, c, llm):
    """跨维度/跨 topic 近邻语义互斥检测（spec §11 第八轮）。

    返回 None（无近邻或不互斥 → 走 ADD）或对端行 key 元组（互斥 → 走 CONFLICT 双存）。
    仅 taboo↔preference 同 scope 配对；对端必须是规范行（排除 ~ / + / ! 变形）。
    """
    dims = _NEIGHBOR_PAIRS.get(c["dimension"])
    if not dims or not llm or not valid_scope(c.get("scope")) or not c.get("quote"):
        return None
    placeholders = ",".join("?" * len(dims))
    rows = conn.execute(
        f"SELECT dimension, topic, scope, id, body, summary FROM entries"
        f" WHERE user_source=? AND user_id=? AND dimension IN ({placeholders})"
        f" AND scope=? AND status='active' AND topic NOT LIKE '~%'"
        f" AND topic NOT LIKE '+%' AND topic NOT LIKE '!%' LIMIT ?",
        (user_source, user_id, *dims, c["scope"], _NEIGHBOR_LIMIT)).fetchall()
    if not rows:
        return None
    neighbors = "\n".join(f"- [{r['id']}] {r['body']}" for r in rows)
    verdict = llm.chat_json(NEIGHBOR_SYSTEM, NEIGHBOR_USER.format(new=c["body"],
                                                                  neighbors=neighbors),
                            NEIGHBOR_SCHEMA)
    if not verdict.get("conflict"):
        return None
    # LLM 指认的对端 id@scope 优先；缺失/不匹配时回退首行
    accused = str(verdict.get("conflicting_with") or "").strip()
    for r in rows:
        if accused in (r["id"], f"{r['id']}@{r['scope']}"):
            return (r["dimension"], r["topic"], r["scope"])
    row = rows[0]
    return (row["dimension"], row["topic"], row["scope"])


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
