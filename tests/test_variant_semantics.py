"""variant 语义回归：召回只看无争议行；已裁决胜出副本仍可注入。"""
import json

from pismor.serving.assembler import assemble

NOW = "2026-11-20T00:00:00+00:00"


def _insert(conn, entry_id, *, variant="normal", variant_seq=0, status="active",
            body="正文", conflict_with=None):
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,variant,variant_seq,"
        "confidence,status,body,summary,created_at,updated_at,last_reinforced_at,"
        "conflict_with) VALUES ('w','u1','taboo','tool',?,?,?,0.9,?,?,?,"
        "?,?,?,?)",
        (entry_id, variant, variant_seq, status, body, body[:20],
         NOW, NOW, NOW, conflict_with))
    conn.commit()


def test_unresolved_conflict_excluded_by_default(conn):
    _insert(conn, "n1")
    _insert(conn, "c1", variant="conflict", variant_seq=1)
    conn.execute("UPDATE entries SET conflict_with=? WHERE id='n1'", (json.dumps(["c1"]),))
    conn.execute("UPDATE entries SET conflict_with=? WHERE id='c1'", (json.dumps(["n1"]),))
    conn.commit()

    # 未裁决：双方都带 conflict_with，默认都不注入
    ids = {e["id"] for e in assemble(conn, None, "w", "u1", budget=2000)["entries"]}
    assert ids == set()

    ids = {e["id"] for e in assemble(
        conn, None, "w", "u1", budget=2000, recall_conflicts=True)["entries"]}
    assert ids == {"n1", "c1"}


def test_resolved_conflict_winner_included(conn):
    # taboo 胜出副本：variant=conflict、active、无争议标记，必须继续注入
    _insert(conn, "c1", variant="conflict", variant_seq=1, body="禁用 Python")
    ids = {e["id"] for e in assemble(conn, None, "w", "u1", budget=2000)["entries"]}
    assert ids == {"c1"}
