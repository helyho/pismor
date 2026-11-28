"""四路决策器：ADD / UPDATE / CONFLICT / NOOP，另对 orphan 候选 ORPHAN 透传。

分流规则（spec §10/§11，第八轮扩展）：
- orphan 候选不调 LLM，直接 ORPHAN 透传（决策 LLM 只判 active 候选）；
- 库中无同 (user, dimension, topic) 规范行 → 近邻语义互斥检测（跨维度/跨 topic
  矛盾，第八轮新增）后 ADD，不进同 key 决策 LLM；
- 其余交决策 LLM，decision 枚举 UPDATE|CONFLICT|NOOP（RESOLVE_SCHEMA 约束）；
- taboo 恒胜特则：preference 候选与同主题 taboo 条目互斥时，无论 LLM 判什么
  都强制 CONFLICT（代码级硬约束，双保险于 prompt 特则句），taboo 侧按正常演进 UPDATE。

近邻互斥检测（spec §11 第八轮：矛盾判定从"同 key"扩展到"语义互斥"）：
- 仅 taboo↔preference 配对（黑盒 A5 高发型：preference_style vs taboo_tone）；
- 同用户最近 5 条近邻候选（`neighbor_candidates` 的 SQL LIMIT 5）；对端行非同 key（同 key 已走决策器）；
- LLM 判互斥 → 走既有 CONFLICT 双存：conflict 变体行 + conflict_with 互标行 id，
  装配 clarification_needed 与复核队列按标记配对（机制零新增）。
"""
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
            "SELECT * FROM entries WHERE user_source=? AND user_id=? AND dimension=? AND topic=?"
            " AND variant='normal' AND status!='archived'",
            (user_source, user_id, c["dimension"], c["topic"])).fetchone()
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
                            "target": (existing["dimension"], existing["topic"])})
            continue
        taboo_row = _same_topic_taboo(conn, user_source, user_id, c)
        resolution = llm.chat_json(RESOLVE_SYSTEM, RESOLVE_USER.format(
            old_dim=existing["dimension"], topic=c["topic"],
            old=existing["body"], new_dim=c["dimension"], new=c["body"],
            taboo=_taboo_note(taboo_row)), RESOLVE_SCHEMA)
        op = resolution.get("decision")
        if c["dimension"] == "preference" and taboo_row:
            op = "CONFLICT"  # taboo 恒胜：preference 侧衰减，覆盖 LLM 判定
        target = (existing["dimension"], existing["topic"])
        if op == "UPDATE":
            actions.append({"op": "UPDATE", "candidate": c, "target": target})
        elif op == "CONFLICT":
            actions.append({"op": "CONFLICT", "candidate": c, "target": target})
        else:
            actions.append({"op": "NOOP", "candidate": c, "target": target})
    return actions


class LlmUnavailable(RuntimeError):
    """strict 模式下近邻互斥检测无法执行（LLM 缺失或调用失败）。

    管线内部路径不传 strict、静默容错；admin 手工录入路径（Task 5 create_entry）
    传 strict=True，以此拒绝创建而非跳过跨维度互斥检查。
    """


def neighbor_candidates(conn, user_source: str, user_id: str, dimension: str) -> list[dict]:
    """近邻互斥检测的候选行：dimension 的对端维度（taboo↔preference）active 规范行。

    status='active' 且 variant='normal'；conflict/trace/snapshot 变体不参与近邻判定。
    dimension 无对端（不在 _NEIGHBOR_PAIRS）→ 空列表。
    """
    dims = _NEIGHBOR_PAIRS.get(dimension)
    if not dims:
        return []
    placeholders = ",".join("?" * len(dims))
    rows = conn.execute(
        f"SELECT dimension, topic, id, body, summary FROM entries"
        f" WHERE user_source=? AND user_id=? AND dimension IN ({placeholders})"
        f" AND status='active' AND variant='normal' LIMIT ?",
        (user_source, user_id, *dims, _NEIGHBOR_LIMIT)).fetchall()
    return [dict(r) for r in rows]


def neighbor_conflict(conn, user_source, user_id, candidate, llm,
                      *, strict: bool = False) -> tuple[str, str] | None:
    """跨维度/跨 topic 近邻语义互斥检测（spec §11 第八轮）。

    返回 None（无近邻、不互斥或 LLM 缺失静默跳过）或对端行 key 元组（互斥 → CONFLICT 双存）。
    仅 taboo↔preference 配对；对端必须是规范行（variant='normal'）。
    strict=True（admin 手工录入路径）：候选行非空但 llm 缺失，或 LLM 调用抛任何异常
    → raise LlmUnavailable（拒绝创建而非跳过互斥检测）；strict=False 维持管线现状语义。
    """
    if not candidate.get("quote"):
        return None
    rows = neighbor_candidates(conn, user_source, user_id, candidate["dimension"])
    if not rows:
        return None
    if llm is None:
        if strict:
            raise LlmUnavailable("近邻互斥检测需要 LLM 但未提供")
        return None
    neighbors = "\n".join(f"- [{r['id']}] {r['body']}" for r in rows)
    try:
        resolution = llm.chat_json(NEIGHBOR_SYSTEM, NEIGHBOR_USER.format(new=candidate["body"],
                                                                      neighbors=neighbors),
                                NEIGHBOR_SCHEMA)
    except Exception as exc:
        if strict:
            raise LlmUnavailable(f"近邻互斥检测 LLM 调用失败：{exc}") from exc
        raise  # strict=False：维持现状语义，异常原样传播
    if not resolution.get("conflict"):
        return None
    # LLM 指认的对端行 id 优先；指认不匹配任何近邻时视为模型幻觉——
    # 回退首行会把无关的 active 行拖进矛盾双存并降级×0.8，宁当轮 ADD 不误伤（F4）
    accused = str(resolution.get("conflicting_with") or "").strip()
    for r in rows:
        if accused == r["id"]:
            return (r["dimension"], r["topic"])
    return None


def _neighbor_conflict(conn, user_source, user_id, c, llm):
    """管线内部入口（thin wrapper）：不传 strict，行为与原实现一致。"""
    return neighbor_conflict(conn, user_source, user_id, c, llm)


def _same_topic_taboo(conn, user_source, user_id, c):
    """同 (user, topic) 下的并存 taboo 规范行；taboo 候选自身不构成互斥。"""
    if c["dimension"] == "taboo":
        return None
    return conn.execute(
        "SELECT id, body FROM entries WHERE user_source=? AND user_id=?"
        " AND dimension='taboo' AND topic=? AND variant='normal'"
        " AND status IN ('active','degraded')",
        (user_source, user_id, c["topic"])).fetchone()


def _taboo_note(row):
    if row is None:
        return "无"
    return f"有（{row['id']}：{row['body']}）——taboo 恒胜，与 taboo 互斥的 preference 判定必须为 CONFLICT"
