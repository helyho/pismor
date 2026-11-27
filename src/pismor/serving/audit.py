"""一致性审计（spec §4/§6/§7/§9 invariants SQL 化）：扫描产 violations；标记动作另置。

规则集（spec §7 invariants + §4 指令扫描 + §6 状态机静态校验 + §9 taboo 文件规则）：
- taboo_no_overlap: 同 user 的 taboo 与 preference 存活行近似同主题（相等/子串/共享词根），
  仅报"矛盾未经裁决"：preference 侧 degraded/conflict_lost 或双方 conflict_with 互标 =
  已裁决终态，排除。
- taboo_negation: taboo 条目每条 quote 必须含否定词（spec §9 文件级 invariant）；
- conflict_with_json: 坏 JSON 行记 violation，绝不中断整次审计（I-12）；
- quote_required / inferred_cap / unique_key / status_consistency /
  instruction_pattern 同前。

violations 每条附 entry 定位（四元组），供 flag_violations 落复核队列。
停用主题仍保留在 topics 中，审计不做词表外判定：存量条目按原状态保留。
run_audit 只读；标记动作由 flag_violations 显式执行（spec §6 审计标记）。
"""
import json

from pismor.core.keys import display_key
from pismor.llm.prompts import INSTRUCTION_RE
from pismor.pipeline.safety import quote_is_negated, topics_near

_ALIVE = ("active", "degraded")  # 存活状态（恒胜/并存判定的范围）
_ENTRY_FIELDS = ("user_source", "user_id", "dimension", "topic")


def run_audit(conn, taxonomy, *, inferred_cap: float = 0.8) -> list[dict]:
    """跑全部规则，返回 [{rule, detail, entry}]；只读，不修改任何数据。

    inferred_cap 与闸门 settings.gate.inferred_cap 同源，避免配置改了审计口径不跟。
    """
    out = []
    out += _taboo_no_overlap(conn)
    out += _taboo_negation(conn)
    out += _conflict_json(conn)
    out += _quote_required(conn)
    out += _inferred_cap(conn, inferred_cap)
    out += _unique_key(conn)
    out += _status_consistency(conn)
    out += _instruction_pattern(conn)
    blocked = {
        (row["user_source"], row["user_id"])
        for row in conn.execute("SELECT user_source, user_id FROM tombstones")
    }
    if blocked:
        out = [
            v for v in out
            if (v["entry"]["user_source"], v["entry"]["user_id"]) not in blocked
        ]
    return out


def _entry(row) -> dict:
    d = {k: row[k] for k in _ENTRY_FIELDS}
    d["id"] = row["id"] if "id" in row.keys() else None
    d["variant"] = row["variant"] if "variant" in row.keys() else "normal"
    d["variant_seq"] = row["variant_seq"] if "variant_seq" in row.keys() else 0
    return d


def _loc(row):
    """违规行定位前缀：user/user_id + 旧观感键（含 variant 形态）。"""
    variant = row["variant"] if "variant" in row.keys() else "normal"
    seq = row["variant_seq"] if "variant_seq" in row.keys() else 0
    return (f"{row['user_source']}/{row['user_id']} "
            f"{display_key(row['dimension'], row['topic'], variant, seq)}")


def _violation(rule, row, detail) -> dict:
    return {"rule": rule, "detail": detail, "entry": _entry(row)}


def _taboo_no_overlap(conn):
    rows = conn.execute(
        "SELECT user_source, user_id, dimension, topic, id, variant, variant_seq,"
        " degraded_reason, conflict_with FROM entries"
        " WHERE dimension IN ('taboo','preference') AND status IN ('active','degraded')"
        " AND variant IN ('normal','conflict')").fetchall()
    taboos = [r for r in rows if r["dimension"] == "taboo"]
    prefs = [r for r in rows if r["dimension"] == "preference"]
    out = []
    for tb in taboos:
        for pf in prefs:
            if (tb["user_source"], tb["user_id"]) != (pf["user_source"], pf["user_id"]):
                continue
            if not topics_near(tb["topic"], pf["topic"]):
                continue
            if _adjudicated(tb, pf):
                continue  # conflict_lost 终态或 conflict_with 互标：已裁决，非违规
            out.append(_violation(
                "taboo_no_overlap", tb,
                f"{_loc(tb)} 与 {pf['dimension']}/{pf['topic']}"
                f" 近似同主题矛盾未经裁决（taboo reason={tb['degraded_reason']},"
                f" preference reason={pf['degraded_reason']}, 无 conflict_with 互标）"))
    return out


def _adjudicated(tb, pf):
    """已裁决：preference 恒胜落败（conflict_lost）或双方互标 conflict_with。

    坏 JSON 不得中断审计（I-12）：解析失败按"未裁决"处理，另有 conflict_with_json 规则报出。
    """
    if pf["degraded_reason"] == "conflict_lost":
        return True
    try:
        tb_marks = set(json.loads(tb["conflict_with"] or "[]"))
        pf_marks = set(json.loads(pf["conflict_with"] or "[]"))
    except (TypeError, ValueError):
        return False
    return tb["id"] in pf_marks or pf["id"] in tb_marks


def _conflict_json(conn):
    out = []
    for r in conn.execute(
            "SELECT user_source, user_id, dimension, topic, id, variant, variant_seq,"
            " conflict_with FROM entries WHERE conflict_with IS NOT NULL").fetchall():
        try:
            marks = json.loads(r["conflict_with"])
            if not isinstance(marks, list):
                raise ValueError("not a list")
        except (TypeError, ValueError):
            out.append(_violation("conflict_with_json", r,
                                  f"{_loc(r)} conflict_with 非合法 JSON 数组"))
    return out


def _taboo_negation(conn):
    out = []
    rows = conn.execute(
        "SELECT e.user_source, e.user_id, e.dimension, e.topic, e.id,"
        " e.variant, e.variant_seq, s.quote"
        " FROM entries e JOIN sources s"
        "  ON s.user_source=e.user_source AND s.user_id=e.user_id"
        " AND s.entry_id = e.id"
        " WHERE e.dimension='taboo' AND e.status IN ('active','degraded')").fetchall()
    for r in rows:
        if not quote_is_negated(r["quote"]):
            out.append(_violation("taboo_negation", r,
                                  f"{_loc(r)} quote 不含否定词：{r['quote']!r}"))
    return out


def _quote_required(conn):
    return [_violation("quote_required", r, f"{_loc(r)} 无证据行") for r in conn.execute(
        "SELECT e.id, e.user_source, e.user_id, e.dimension, e.topic,"
        " e.variant, e.variant_seq"
        " FROM entries e LEFT JOIN sources s"
        "  ON s.user_source=e.user_source AND s.user_id=e.user_id"
        " AND s.entry_id = e.id"
        " WHERE s.seq IS NULL AND e.variant <> 'trace'").fetchall()]


def _inferred_cap(conn, cap: float):
    return [_violation("inferred_cap", r,
                       f"{_loc(r)} inferred confidence {r['confidence']} > {cap}")
            for r in conn.execute(
        "SELECT DISTINCT e.id, e.user_source, e.user_id, e.dimension, e.topic,"
        " e.variant, e.variant_seq, e.confidence"
        " FROM entries e JOIN sources s"
        "  ON s.user_source=e.user_source AND s.user_id=e.user_id"
        " AND s.entry_id = e.id"
        " WHERE s.kind='inferred' AND e.confidence > ?", (cap,)).fetchall()]


def _unique_key(conn):
    return [_violation("unique_key", r, f"{_loc(r)} 六元组重复 {r['c']} 行")
            for r in conn.execute(
        "SELECT user_source, user_id, dimension, topic, variant, variant_seq, COUNT(*) c"
        " FROM entries GROUP BY user_source, user_id, dimension, topic, variant, variant_seq"
        " HAVING c > 1").fetchall()]


def _status_consistency(conn):
    # (SQL 条件, 问题描述)；正列=必有特征，反列=字段越界/前缀态互斥
    checks = [
        ("status='degraded' AND degraded_reason IS NULL", "degraded 缺 degraded_reason"),
        ("status='archived' AND archived_at IS NULL", "archived 缺 archived_at"),
        ("status='orphan_topic' AND topic_new IS NULL", "orphan_topic 缺 topic_new"),
        ("status='active' AND degraded_reason IS NOT NULL", "active 残留 degraded_reason"),
        ("status!='archived' AND archived_at IS NOT NULL", "archived_at 越界（仅 archived 可有）"),
        # pending_review 的 orphan 候选按设计带 topic_new（复核通过才转 orphan_topic）
        ("status NOT IN ('orphan_topic','pending_review') AND topic_new IS NOT NULL",
         "topic_new 越界（仅 orphan_topic 可有）"),
        ("variant='trace' AND status!='archived'", "轨迹行必 archived"),
        ("variant='trace' AND degraded_reason IS NOT 'superseded'",
         "轨迹行 degraded_reason 必 superseded"),
        ("variant='conflict' AND status NOT IN ('active','degraded','archived')",
         "conflict 行非法状态"),
        ("variant='snapshot' AND status!='pending_review'", "快照行必 pending_review"),
        ("variant='normal' AND variant_seq<>0", "normal 行 seq 必为 0"),
    ]
    out = []
    for cond, problem in checks:
        rows = conn.execute(
            f"SELECT id, user_source, user_id, dimension, topic, variant, variant_seq"
            f" FROM entries WHERE {cond}"
        ).fetchall()
        out += [_violation("status_consistency", r, f"{_loc(r)} {problem}") for r in rows]
    return out


def _instruction_pattern(conn):
    out = []
    for r in conn.execute(
            "SELECT id, user_source, user_id, dimension, topic, variant, variant_seq,"
            " body FROM entries").fetchall():
        if INSTRUCTION_RE.search(r["body"]):
            out.append(_violation("instruction_pattern", r, f"{_loc(r)} 正文命中指令模式"))
    return out


def flag_violations(conn, violations: list[dict], now: str) -> int:
    """审计标记：命中违规的 active 条目转 pending_review + review_history 留痕。

    已是 pending/orphan/degraded/archived 的行不重复标记（幂等）。
    """
    flagged = 0
    with conn:
        for v in violations:
            e = v.get("entry")
            if not e or not e.get("id"):
                continue
            if conn.execute(
                "SELECT 1 FROM tombstones WHERE user_source=? AND user_id=?",
                (e["user_source"], e["user_id"]),
            ).fetchone():
                continue
            row = conn.execute(
                "SELECT status, updated_at FROM entries WHERE user_source=? AND user_id=?"
                " AND id=?", (e["user_source"], e["user_id"], e["id"])
            ).fetchone()
            if row is None or row["status"] != "active":
                continue
            cur = conn.execute(
                "UPDATE entries SET status='pending_review', updated_at=?"
                " WHERE user_source=? AND user_id=? AND id=?"
                " AND status='active' AND updated_at=?",
                (now, e["user_source"], e["user_id"], e["id"], row["updated_at"]))
            if cur.rowcount == 0:
                continue
            conn.execute(
                "INSERT INTO review_history (entry_id, user_source, user_id, decision, actor,"
                " ts, note, entry_version) VALUES (?, ?, ?, 'flagged', 'audit', ?, ?, ?)",
                (e["id"], e["user_source"], e["user_id"], now,
                 v["rule"], row["updated_at"]))
            flagged += 1
    return flagged
