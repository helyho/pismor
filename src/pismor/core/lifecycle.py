"""条目生命周期规则：状态迁移边表与可见性/可召回谓词。

纯规则 module：不 import sqlite3、不执行 SQL。谓词以字符串片段返回，
由调用方拼进查询；状态迁移只做判断，UPDATE 仍由写入方执行。

设计要点（见 docs/superpowers/specs/2026-11-23-user-profile-memory-design.md §6）：

- ``auto``：管线自动写入，只允许 spec §6 状态机图里的边。
- ``manual``：人工裁决（管理端编辑、复核），当前产品允许任意状态互转——
  编辑弹窗提供全部 5 个状态，创建重复条目时也明确提示「已归档，请走编辑恢复」。
  这里用全集表达现状，并由 tests/core/test_lifecycle.py 逐对锁定；
  将来要收紧某条边，改成显式集合并补一个拒绝用例即可。
"""
from __future__ import annotations

STATUSES = (
    "pending_review",
    "orphan_topic",
    "active",
    "degraded",
    "archived",
)

# 管线自动写入（spec §6 状态机图）；含 deleted 终态。
# active -> pending_review 是审计标记把存量条目打回复核（spec §6 `[*] --> pending_review`）。
AUTO_EDGES = frozenset({
    ("active", "degraded"), ("active", "archived"), ("active", "pending_review"),
    ("degraded", "active"), ("degraded", "archived"),
    ("pending_review", "active"), ("pending_review", "deleted"),
    ("orphan_topic", "active"), ("orphan_topic", "deleted"),
})

# 人工裁决：显式声明为当前允许的全集。
MANUAL_EDGES = AUTO_EDGES | frozenset(
    (frm, to) for frm in STATUSES for to in STATUSES
)


class IllegalTransition(Exception):
    """状态机之外的迁移；writer 也用它表示写入目标缺失。"""


def check_transition(path: str, frm: str, to: str) -> None:
    """校验一次状态迁移。``path`` 取 ``"auto"`` 或 ``"manual"``。"""
    if path == "auto":
        edges = AUTO_EDGES
    elif path == "manual":
        edges = MANUAL_EDGES
    else:
        raise ValueError(f"unknown transition path: {path!r}")
    if (frm, to) not in edges:
        raise IllegalTransition(f"illegal {path} transition: {frm} -> {to}")


def is_tombstoned(conn, user_source: str, user_id: str) -> bool:
    """该用户是否处于删除状态（soft delete 与 purge 都写 tombstones）。"""
    return conn.execute(
        "SELECT 1 FROM tombstones WHERE user_source=? AND user_id=?",
        (user_source, user_id),
    ).fetchone() is not None


def alive_sql(alias: str = "entries") -> str:
    """排除已删除用户（soft delete 与 purge 都写 tombstones）。"""
    return (
        "NOT EXISTS (SELECT 1 FROM tombstones t"
        f" WHERE t.user_source={alias}.user_source AND t.user_id={alias}.user_id)"
    )


def recallable_sql(alias: str = "entries", *, conflict_mode: str = "settled") -> str:
    """可召回条目。

    ``conflict_mode="settled"``（默认）只注入无未消解冲突的行；
    ``"all"`` 退回旧行为，不检查 ``conflict_with``。
    """
    if conflict_mode not in ("settled", "all"):
        raise ValueError(f"unknown conflict_mode: {conflict_mode!r}")
    sql = (
        f"{alias}.status IN ('active','degraded')"
        f" AND {alias}.variant IN ('normal','conflict')"
    )
    if conflict_mode == "settled":
        sql += f" AND {alias}.conflict_with IS NULL"
    return sql


def pending_conflict_sql(alias: str = "entries") -> str:
    """复核队列条件：待复核行 + 未消解的 conflict 形态矛盾双存行。"""
    return (
        f"({alias}.status='pending_review' OR ({alias}.variant='conflict'"
        f" AND {alias}.status IN ('active','degraded')"
        f" AND {alias}.conflict_with IS NOT NULL))"
    )


def queue_condition(alias: str = "entries") -> str:
    """复核队列的完整条件：存活用户 + 待复核/未消解冲突。"""
    return f"{alive_sql(alias)} AND {pending_conflict_sql(alias)}"


def is_recallable(
    status: str,
    variant: str,
    *,
    has_conflict: bool = False,
    conflict_mode: str = "settled",
) -> bool:
    """与 :func:`recallable_sql` 同义的 Python 侧判断。"""
    if conflict_mode not in ("settled", "all"):
        raise ValueError(f"unknown conflict_mode: {conflict_mode!r}")
    if status not in ("active", "degraded"):
        return False
    if variant not in ("normal", "conflict"):
        return False
    if conflict_mode == "settled" and has_conflict:
        return False
    return True
