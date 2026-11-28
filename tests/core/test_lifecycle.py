"""core/lifecycle.py 的单测：边表、谓词文本、Python 侧同义判断。"""
import pytest

from pismor.core import lifecycle as lc


EXPECTED_AUTO = {
    ("active", "degraded"), ("active", "archived"), ("active", "pending_review"),
    ("degraded", "active"), ("degraded", "archived"),
    ("pending_review", "active"), ("pending_review", "deleted"),
    ("orphan_topic", "active"), ("orphan_topic", "deleted"),
}


def test_auto_edges_match_spec_section_6():
    assert lc.AUTO_EDGES == frozenset(EXPECTED_AUTO)


def test_manual_edges_cover_every_status_pair():
    expected = {(frm, to) for frm in lc.STATUSES for to in lc.STATUSES}
    assert lc.MANUAL_EDGES >= expected


def test_manual_edges_are_auto_plus_manual_pairs():
    expected = frozenset(EXPECTED_AUTO) | {
        (frm, to) for frm in lc.STATUSES for to in lc.STATUSES
    }
    assert lc.MANUAL_EDGES == expected


@pytest.mark.parametrize("frm,to", [
    ("archived", "active"),
    ("archived", "degraded"),
    ("pending_review", "orphan_topic"),
    ("active", "active"),
])
def test_auto_rejects_edges_outside_spec(frm, to):
    with pytest.raises(lc.IllegalTransition):
        lc.check_transition("auto", frm, to)


@pytest.mark.parametrize("frm,to", [
    ("archived", "active"),
    ("archived", "degraded"),
    ("active", "pending_review"),
    ("degraded", "orphan_topic"),
])
def test_manual_allows_human_corrections(frm, to):
    lc.check_transition("manual", frm, to)


def test_auto_allows_spec_edges():
    for frm, to in EXPECTED_AUTO:
        lc.check_transition("auto", frm, to)


def test_unknown_path_is_a_programming_error():
    with pytest.raises(ValueError):
        lc.check_transition("guess", "active", "degraded")


def test_alive_sql_uses_alias_everywhere():
    sql = lc.alive_sql("e2")
    assert "FROM tombstones" in sql
    assert "e2.user_source" in sql
    assert "e2.user_id" in sql
    assert "entries." not in sql


def test_recallable_sql_settled_excludes_conflicts():
    sql = lc.recallable_sql("x")
    assert "x.status IN ('active','degraded')" in sql
    assert "x.variant IN ('normal','conflict')" in sql
    assert "x.conflict_with IS NULL" in sql


def test_recallable_sql_all_keeps_conflicts():
    sql = lc.recallable_sql("x", conflict_mode="all")
    assert "conflict_with" not in sql


def test_recallable_sql_rejects_unknown_mode():
    with pytest.raises(ValueError):
        lc.recallable_sql("x", conflict_mode="maybe")


def test_pending_conflict_sql_has_both_branches():
    sql = lc.pending_conflict_sql("q")
    assert "q.status='pending_review'" in sql
    assert "q.variant='conflict'" in sql
    assert "q.conflict_with IS NOT NULL" in sql


def test_queue_condition_combines_alive_and_pending():
    sql = lc.queue_condition("e")
    assert sql.startswith(lc.alive_sql("e"))
    assert lc.pending_conflict_sql("e") in sql


@pytest.mark.parametrize("status,variant,has_conflict,expected", [
    ("active", "normal", False, True),
    ("degraded", "conflict", False, True),
    ("degraded", "conflict", True, False),
    ("pending_review", "normal", False, False),
    ("archived", "normal", False, False),
    ("active", "trace", False, False),
])
def test_is_recallable_truth_table(status, variant, has_conflict, expected):
    assert lc.is_recallable(status, variant, has_conflict=has_conflict) is expected


def test_is_recallable_all_mode_keeps_conflicts():
    assert lc.is_recallable(
        "active", "conflict", has_conflict=True, conflict_mode="all"
    ) is True
