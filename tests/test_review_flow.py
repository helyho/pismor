"""review_flow：一条用例编排，两个入口共用。"""

from pismor.serving import review_flow
from pismor.serving.review_flow import DecisionRequest, ReviewRuntime, execute_decision

VERSION = "2026-01-01T00:00:00+00:00"


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.1, 0.2]]


class FakeStore:
    def __init__(self):
        self.replaced = []

    def replace(self, taxonomy):
        self.replaced.append(taxonomy)


def _seed(conn, *, body="旧正文", topic="手工模型", status="pending_review"):
    eid = f"preference_{topic}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements)"
        " VALUES ('webchat','u1','preference',?,?,0.9,?,?,'s',?,?,?,0)",
        (topic, eid, status, body, VERSION, VERSION, VERSION),
    )
    conn.commit()
    return eid


def _request(eid, **overrides):
    base = dict(
        user_source="webchat", user_id="u1", entry_id=eid,
        decision="confirm", entry_version=VERSION,
    )
    base.update(overrides)
    return DecisionRequest(**base)


def _runtime(taxonomy, **overrides):
    base = dict(taxonomy=taxonomy, embedder=None, backup_root=None, backup_keep=2)
    base.update(overrides)
    return ReviewRuntime(**base)


def test_fix_with_changed_body_precomputes_vector(conn, taxonomy):
    eid = _seed(conn)
    embedder = FakeEmbedder()
    runtime = _runtime(taxonomy, embedder=embedder)
    result = execute_decision(
        conn, _request(eid, decision="fix", body="新正文"), runtime)
    assert result["noop"] is False
    assert embedder.calls == [["新正文"]]


def test_fix_with_same_body_skips_embedder(conn, taxonomy):
    eid = _seed(conn)
    embedder = FakeEmbedder()
    runtime = _runtime(taxonomy, embedder=embedder)
    execute_decision(conn, _request(eid, decision="fix", body="旧正文"), runtime)
    assert embedder.calls == []


def test_confirm_skips_embedder(conn, taxonomy):
    eid = _seed(conn)
    embedder = FakeEmbedder()
    runtime = _runtime(taxonomy, embedder=embedder)
    execute_decision(conn, _request(eid), runtime)
    assert embedder.calls == []


def test_in_txn_hook_runs_inside_transaction(conn, taxonomy):
    eid = _seed(conn)
    seen = []

    def hook(conn_, result):
        seen.append((conn_.in_transaction, result["noop"]))

    execute_decision(conn, _request(eid), _runtime(taxonomy, in_txn=hook))
    assert seen == [(True, False)]


def test_pending_alias_is_committed_after_transaction(conn, taxonomy):
    canon = taxonomy.dimensions["preference"].topics[0]
    eid = _seed(conn)
    store = FakeStore()
    alias_seen_in_txn = []

    def hook(conn_, result):
        alias_seen_in_txn.append(list(store.replaced))

    result = execute_decision(
        conn,
        _request(eid, topic=canon),
        _runtime(taxonomy, taxonomy_store=store, in_txn=hook),
    )
    assert result["rekeyed_to"] == canon
    assert alias_seen_in_txn == [[]]
    assert len(store.replaced) == 1


def test_alias_write_failure_keeps_committed_decision(conn, taxonomy, monkeypatch):
    canon = taxonomy.dimensions["preference"].topics[0]
    eid = _seed(conn)

    def boom(*args, **kwargs):
        raise RuntimeError("file write failed")

    monkeypatch.setattr(review_flow, "write_pending_alias", boom)
    result = execute_decision(
        conn, _request(eid, topic=canon), _runtime(taxonomy))
    assert result["rekeyed_to"] == canon
    row = conn.execute(
        "SELECT topic FROM entries WHERE id=?", (eid,)).fetchone()
    assert row is None or row["topic"] == canon


def test_noop_on_version_mismatch(conn, taxonomy):
    eid = _seed(conn)
    result = execute_decision(
        conn, _request(eid, entry_version="2026-01-02T00:00:00+00:00"),
        _runtime(taxonomy))
    assert result["noop"] is True
