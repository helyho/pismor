"""管线编排失败口径：失败必须回滚"已见"标记，重发同一事件可重放。

背景（审查 #1）：intake 先 commit events_seen 再入队后台任务，若 run_pipeline 吞掉异常
且不撤销标记，LLM/嵌入 的瞬时故障会让事件被永久判重丢失。
"""
from pathlib import Path

import pytest

from pismor.core.config import EmbeddingSettings, LLMSettings, Settings
from pismor.pipeline import runner
from pismor.pipeline.intake import intake_event

REPO = Path(__file__).resolve().parent.parent
NOW = "2026-11-24T10:00:00+08:00"


def event(event_id="e1"):
    return {"source": "webchat", "user_ref": "u1", "event_id": event_id,
            "event_type": "conversation", "session_id": "s1", "turn": 1, "ts": NOW,
            "messages": [{"role": "user", "text": "回答短一点"},
                         {"role": "assistant", "text": "好的"}]}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "runner.db",
        taxonomy_path=REPO / "config" / "taxonomy.yaml",
        llm=LLMSettings(endpoint="http://llm.test", model="m", api_key_env="LLM_API_KEY"),
        embedding=EmbeddingSettings(enabled=False, endpoint="", model="",
                                    api_key_env="", dimensions=4, batch_size=8),
    )


def _seen(conn):
    return conn.execute("SELECT COUNT(*) c FROM events_seen").fetchone()["c"]


def test_failure_returns_false_and_clears_seen_marker(conn, settings, taxonomy, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("llm 429")

    monkeypatch.setattr(runner, "_run", boom)
    ok, _ = intake_event(conn, event())
    assert ok is True and _seen(conn) == 1

    assert runner.run_pipeline(conn, settings, event(), taxonomy=taxonomy, llm=object()) is False
    assert _seen(conn) == 0  # 撤销已见标记 → 允许重放
    # 同一事件重发不再被判重
    ok2, _ = intake_event(conn, event())
    assert ok2 is True


def test_success_returns_true_and_keeps_seen_marker(conn, settings, taxonomy, monkeypatch):
    called = []

    def ok_run(*args, **kwargs):
        called.append(args[2]["event_id"])

    monkeypatch.setattr(runner, "_run", ok_run)
    intake_event(conn, event())
    assert runner.run_pipeline(conn, settings, event(), taxonomy=taxonomy, llm=object()) is True
    assert called == ["e1"]
    assert _seen(conn) == 1
