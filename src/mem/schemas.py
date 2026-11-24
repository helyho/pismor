from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
import hashlib, json


class _Base(BaseModel):
    source: str = Field(min_length=1)
    user_ref: str = Field(min_length=1)
    event_id: str | None = None
    ts: str

    @field_validator("ts")
    @classmethod
    def _validate_ts(cls, value: str) -> str:
        """ts 必须是带时区的 ISO 8601，否则 julianday 归一会得到 NULL，降级/归档静默失效。"""
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ts must be ISO 8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("ts must include timezone offset")
        return value


def canonical_hash(ev: dict) -> str:
    payload = json.dumps({k: ev.get(k) for k in ("source", "user_ref", "ts", "content", "messages")},
                         ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class ConversationEvent(_Base):
    event_type: Literal["conversation"] = "conversation"
    session_id: str
    turn: int
    messages: list[dict] = Field(min_length=1)  # [{"role": "user"|"assistant", "text": str}]
    feedback: list[dict] = []

    @field_validator("messages")
    @classmethod
    def _validate_messages(cls, value: list[dict]) -> list[dict]:
        for message in value:
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            role = message.get("role")
            text = message.get("text")
            if role not in ("user", "assistant"):
                raise ValueError("message.role must be user|assistant")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("message.text must be a non-empty string")
        return value


class TextEvent(_Base):
    event_type: Literal["text"] = "text"
    content: str = Field(min_length=1, max_length=8192)
    title: str | None = None
    origin_ref: str | None = None


EventIn = ConversationEvent | TextEvent

def resolve_event_id(ev: dict) -> str:
    return ev.get("event_id") or canonical_hash(ev)
