from typing import Literal

from pydantic import BaseModel, Field
import hashlib, json

class _Base(BaseModel):
    source: str
    user_ref: str
    event_id: str | None = None
    ts: str

def canonical_hash(ev: dict) -> str:
    payload = json.dumps({k: ev.get(k) for k in ("source", "user_ref", "ts", "content", "messages")},
                         ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

class ConversationEvent(_Base):
    event_type: Literal["conversation"] = "conversation"
    session_id: str
    turn: int
    messages: list[dict]  # [{"role": "user"|"assistant", "text": str}]
    feedback: list[dict] = []

class TextEvent(_Base):
    event_type: Literal["text"] = "text"
    content: str = Field(max_length=8192)
    title: str | None = None
    origin_ref: str | None = None

EventIn = ConversationEvent | TextEvent

def resolve_event_id(ev: dict) -> str:
    return ev.get("event_id") or canonical_hash(ev)
