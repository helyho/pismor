"""一致性审计（spec §4/§6/§7 invariants SQL 化）：只读扫描，产 violations，不改数据。

规则集（spec §7 invariants + §4 指令扫描 + §6 状态机静态校验）：
- taboo_no_overlap: 同 user 的 taboo 与 preference 存活行近似同主题（相等/子串/共享词根）
  且 scope 相容（相等或一方 global），仅报"矛盾未经裁决"（修复轮 1 裁决）：
  preference 侧 degraded/conflict_lost 或双方 conflict_with 互标 = spec §11 恒胜已裁决
  终态，排除；双方 active、或 preference degraded 但 reason≠conflict_lost，且无互标 → 违规。
  ——SQL 拉行 + Python 近似判定。
- quote_required: entries 左连 sources 无证据行（~ 轨迹行豁免：证据链已转移至自身键，
  不因归档时间线差异误报）。
- inferred_cap: sources.kind='inferred' 且 entry confidence>0.8（上限含 0.8 合法）。
- unique_key_per_scope: 同五元组重复——主键已保证，防御性校验防未来 schema 变更。
- status_consistency: 非法组合全列（degraded 必有 reason、archived 必有 archived_at、
  orphan 必有 topic_new、active 不残留降级字段、archived_at/topic_new/轨迹行前缀/
  '+' 变形行的状态互斥）。
- instruction_pattern: 全表 body 扫 INSTRUCTION_RE（含 ~ 轨迹行，与 extractor 同一常量）。

taxonomy 参数为接口契约保留：当前规则集是 spec 固定不变量，不依赖词表内容。
"""
import json
import re

from mem.prompts import INSTRUCTION_RE

_ALIVE = ("active", "degraded")  # 存活状态（恒胜/并存判定的范围）


def run_audit(conn, taxonomy) -> list[dict]:
    """跑全部规则，返回 [{rule, detail}]；只读，不修改任何数据。"""
    out = []
    out += _taboo_no_overlap(conn)
    out += _quote_required(conn)
    out += _inferred_cap(conn)
    out += _unique_key(conn)
    out += _status_consistency(conn)
    out += _instruction_pattern(conn)
    return out


def _loc(row):
    """违规行定位前缀：user/user_id dim/topic@scope。"""
    return f"{row['user_source']}/{row['user_id']} {row['dimension']}/{row['topic']}@{row['scope']}"


def _taboo_no_overlap(conn):
    rows = conn.execute(
        "SELECT user_source, user_id, dimension, topic, scope, id, degraded_reason,"
        " conflict_with FROM entries"
        " WHERE dimension IN ('taboo','preference') AND status IN ('active','degraded')").fetchall()
    taboos = [r for r in rows if r["dimension"] == "taboo"]
    prefs = [r for r in rows if r["dimension"] == "preference"]
    out = []
    for tb in taboos:
        for pf in prefs:
            if (tb["user_source"], tb["user_id"]) != (pf["user_source"], pf["user_id"]):
                continue
            if not _scope_compatible(tb["scope"], pf["scope"]):
                continue
            if not _topics_near(tb["topic"], pf["topic"]):
                continue
            if _adjudicated(tb, pf):
                continue  # conflict_lost 终态或 conflict_with 互标：spec §11 已裁决，非违规
            out.append({"rule": "taboo_no_overlap", "detail":
                        f"{_loc(tb)} 与 {pf['dimension']}/{pf['topic']}@{pf['scope']}"
                        f" 近似同主题矛盾未经裁决（taboo reason={tb['degraded_reason']},"
                        f" preference reason={pf['degraded_reason']}, 无 conflict_with 互标）"})
    return out


def _adjudicated(tb, pf):
    """已裁决：preference 恒胜落败（conflict_lost）或双方互标 conflict_with。"""
    if pf["degraded_reason"] == "conflict_lost":
        return True
    marked = lambda r: set(json.loads(r["conflict_with"] or "[]"))  # noqa: E731
    return tb["id"] in marked(pf) or pf["id"] in marked(tb)


def _scope_compatible(a, b):
    return a == b or a == "global" or b == "global"


def _topics_near(a, b):
    """近似同主题：剥 ~/+ 前缀后相等、一方为另一方子串、或共享词根（分隔符首段）。"""
    a = a.lstrip("~+")
    b = b.lstrip("~+")
    if a == b or a in b or b in a:
        return True
    root = lambda t: re.split(r"[\W_]+", t)[0]  # noqa: E731 首段词根
    return bool(root(a)) and root(a) == root(b)


def _quote_required(conn):
    return [{"rule": "quote_required", "detail": f"{_loc(r)} 无证据行"} for r in conn.execute(
        "SELECT e.user_source, e.user_id, e.dimension, e.topic, e.scope"
        " FROM entries e LEFT JOIN sources s"
        "  ON s.user_source=e.user_source AND s.user_id=e.user_id"
        " AND s.entry_id = e.dimension || '_' || e.topic || '@' || e.scope"
        " WHERE s.seq IS NULL AND e.topic NOT LIKE '~%'").fetchall()]


def _inferred_cap(conn):
    return [{"rule": "inferred_cap", "detail": f"{_loc(r)} inferred confidence {r['confidence']} > 0.8"}
            for r in conn.execute(
        "SELECT DISTINCT e.user_source, e.user_id, e.dimension, e.topic, e.scope, e.confidence"
        " FROM entries e JOIN sources s"
        "  ON s.user_source=e.user_source AND s.user_id=e.user_id"
        " AND s.entry_id = e.dimension || '_' || e.topic || '@' || e.scope"
        " WHERE s.kind='inferred' AND e.confidence > 0.8").fetchall()]


def _unique_key(conn):
    return [{"rule": "unique_key_per_scope", "detail": f"{_loc(r)} 五元组重复 {r['c']} 行"}
            for r in conn.execute(
        "SELECT user_source, user_id, dimension, topic, scope, COUNT(*) c FROM entries"
        " GROUP BY user_source, user_id, dimension, topic, scope HAVING c > 1").fetchall()]


def _status_consistency(conn):
    # (SQL 条件, 问题描述)；正列=必有特征，反列=字段越界/前缀态互斥
    checks = [
        ("status='degraded' AND degraded_reason IS NULL", "degraded 缺 degraded_reason"),
        ("status='archived' AND archived_at IS NULL", "archived 缺 archived_at"),
        ("status='orphan_topic' AND topic_new IS NULL", "orphan_topic 缺 topic_new"),
        ("status='active' AND degraded_reason IS NOT NULL", "active 残留 degraded_reason"),
        ("status!='archived' AND archived_at IS NOT NULL", "archived_at 越界（仅 archived 可有）"),
        ("status!='orphan_topic' AND topic_new IS NOT NULL", "topic_new 越界（仅 orphan_topic 可有）"),
        ("topic LIKE '~%' AND status!='archived'", "轨迹行必 archived"),
        ("topic LIKE '+%' AND status NOT IN ('active','degraded')", "'+' 变形行非法状态"),
    ]
    out = []
    for cond, problem in checks:
        rows = conn.execute(
            f"SELECT user_source, user_id, dimension, topic, scope FROM entries WHERE {cond}"
        ).fetchall()
        out += [{"rule": "status_consistency", "detail": f"{_loc(r)} {problem}"} for r in rows]
    return out


def _instruction_pattern(conn):
    out = []
    for r in conn.execute(
            "SELECT user_source, user_id, dimension, topic, scope, body FROM entries").fetchall():
        if INSTRUCTION_RE.search(r["body"]):
            out.append({"rule": "instruction_pattern", "detail": f"{_loc(r)} 正文命中指令模式"})
    return out
