from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
import hashlib, json
import re

# source/user_ref 是五元组主键与 vec_key（\x1f 连接）的成分：控制字符会让不同用户
# 合成同一个键，检索互相串扰（F5）。入库侧只拦新事件，存量数据不受影响。
_KEY_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class _Base(BaseModel):
    source: str = Field(min_length=1, max_length=128)
    user_ref: str = Field(min_length=1, max_length=128)
    event_id: str | None = None
    ts: str

    @field_validator("source", "user_ref")
    @classmethod
    def _validate_key_safe(cls, value: str) -> str:
        if _KEY_UNSAFE_CHARS.search(value):
            raise ValueError("must not contain control characters")
        return value

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
    # 上报方判定本轮值得抽取；None 等同 false
    force_extract: bool | None = None

    @field_validator("messages")
    @classmethod
    def _validate_messages(cls, value: list[dict]) -> list[dict]:
        has_user = False
        for message in value:
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            role = message.get("role")
            text = message.get("text")
            if role not in ("user", "assistant"):
                raise ValueError("message.role must be user|assistant")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("message.text must be a non-empty string")
            has_user = has_user or role == "user"
        if not has_user:
            raise ValueError("messages must contain at least one user message")
        return value


class TextEvent(_Base):
    event_type: Literal["text"] = "text"
    content: str = Field(min_length=1)  # 上限 max_length 由 intake 按 settings.intake 校验
    title: str | None = None
    origin_ref: str | None = None


EventIn = ConversationEvent | TextEvent

def resolve_event_id(ev: dict) -> str:
    return ev.get("event_id") or canonical_hash(ev)
