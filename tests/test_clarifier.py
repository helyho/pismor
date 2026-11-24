"""澄清器测试：question 发出落库、回话关联判定、TTL 过期选择性与无关消息不误关联。"""
import json

import pytest

from mem.clarifier import expire, issue, match_incoming


class ScriptedLLM:
    """按脚本回复；记录调用以断言关联 LLM 是否被触发。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat_json(self, system, user, schema):
        self.calls.append((system, user, schema))
        return self.reply


@pytest.fixture
def fake_llm_yes():
    return ScriptedLLM({"is_answer": True})


@pytest.fixture
def fake_llm_no():
    return ScriptedLLM({"is_answer": False})


@pytest.fixture
def seeded_pending(conn):
    issue(conn, "w", "u", ["a", "b"], {"a": "要正式书面", "b": "要口语化"})
    return conn


@pytest.fixture
def seeded_expired_pending(conn):
    # 一行远超 TTL（应删）+ 一行 1 天前（应留），验证 expire 的选择性
    conn.executemany(
        "INSERT INTO pending_clarifications"
        " (user_source, user_id, conflicting_ids, question, issued_at)"
        " VALUES ('w','u',?,?,?)",
        [('[\"a\",\"b\"]', "旧问题1", "2026-11-01T00:00:00+00:00"),
         ('[\"c\",\"d\"]', "旧问题2", "2026-11-30T00:00:00+00:00")])
    conn.commit()
    return conn


def test_issue_and_match(conn, fake_llm_yes):
    q = issue(conn, "w", "u", ["a", "b"], {"a": "要正式书面", "b": "要口语化"})
    assert "正式" in q and "口语化" in q
    out = match_incoming(conn, "w", "u", "平时聊天随意点，写东西再正式", fake_llm_yes)
    assert out["conflicting_ids"] == ["a", "b"]
    assert out["answer"] == "平时聊天随意点，写东西再正式"
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 0


def test_issue_persists_row(conn):
    issue(conn, "w", "u", ["a", "b"], {"a": "要正式书面", "b": "要口语化"})
    row = conn.execute("SELECT * FROM pending_clarifications").fetchone()
    assert row["user_source"] == "w" and row["user_id"] == "u"
    assert json.loads(row["conflicting_ids"]) == ["a", "b"]
    assert row["ttl_days"] == 7


def test_no_pending_returns_none_without_llm(conn, fake_llm_yes):
    assert match_incoming(conn, "w", "u", "今天天气不错", fake_llm_yes) is None
    assert fake_llm_yes.calls == []  # 无 pending 不进 LLM


def test_irrelevant_message_returns_none(conn, fake_llm_no, seeded_pending):
    assert match_incoming(conn, "w", "u", "今天天气不错", fake_llm_no) is None
    # 判否后 pending 保留，等后续消息再判
    assert conn.execute("SELECT COUNT(*) c FROM pending_clarifications").fetchone()["c"] == 1


def test_expire_ttl(conn, seeded_expired_pending):
    assert expire(conn, "2026-12-01") == 1
    remain = conn.execute("SELECT question FROM pending_clarifications").fetchall()
    assert [r["question"] for r in remain] == ["旧问题2"]
