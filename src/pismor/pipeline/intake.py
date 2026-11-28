import json
import sqlite3
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError
from pismor.core.schemas import ConversationEvent, EventIn, resolve_event_id

adapter = TypeAdapter(EventIn)


class ContentTooLong(ValueError):
    """事件体积超过 intake 上限（api 层转 422）。"""


def _check_conversation_limits(ev: ConversationEvent, *,
                               max_messages: int,
                               max_message_chars: int,
                               max_total_chars: int) -> None:
    """conversation 事件的体积上限；超限抛 ContentTooLong，先于 events_seen 落库。"""
    messages = ev.messages
    if len(messages) > max_messages:
        raise ContentTooLong(
            f"messages: 条数 {len(messages)} 超过上限 {max_messages}")
    total = 0
    for idx, message in enumerate(messages):
        text = message.get("text") or ""
        if len(text) > max_message_chars:
            raise ContentTooLong(
                f"messages[{idx}].text: 长度 {len(text)} 超过上限 {max_message_chars}")
        total += len(text)
    if total > max_total_chars:
        raise ContentTooLong(
            f"messages: 总长度 {total} 超过上限 {max_total_chars}")


def intake_event(conn, event: dict, *, max_content_chars: int = 8192,
                 max_messages: int = 100, max_message_chars: int = 8192,
                 max_total_chars: int = 65536) -> tuple[bool, dict]:
    # 畸形输入抛 pydantic.ValidationError（校验先于落库，不写 events_seen）
    ev = adapter.validate_python(event)
    # 归一化回写：管线消费的是原始 dict，不能依赖 pydantic 的 lax 转换与默认值。
    # 否则 "False" 这类字符串在 trigger 里是 truthy，event_type 缺省会被当成 text。
    event["event_type"] = ev.event_type
    if isinstance(ev, ConversationEvent):
        event["force_extract"] = ev.force_extract is True
        _check_conversation_limits(
            ev, max_messages=max_messages,
            max_message_chars=max_message_chars, max_total_chars=max_total_chars)
    content = getattr(ev, "content", None)
    if content is not None:
        # text 事件 content 上限可配（settings.intake.max_content_chars）；复用 pydantic 校验保证 422 语义
        TypeAdapter(Annotated[str, Field(max_length=max_content_chars)]).validate_python(content)
    event_id = resolve_event_id(event)
    # hash 兜底 event_id 回写事件本体：管线 origin_ref 不得为空（spec §5 证据链可定位）
    event["event_id"] = event_id
    try:
        cur = conn.execute("INSERT INTO events_seen (source, event_id, ts) VALUES (?,?,?)",
                           (ev.source, event_id, ev.ts))
    except sqlite3.IntegrityError:
        conn.rollback()
        return False, {"deduplicated": True}
    if cur.rowcount == 0:
        conn.rollback()
        return False, {"deduplicated": True}
    conn.commit()
    payload = json.dumps(event, ensure_ascii=False)
    return True, {"event_id": event_id, "payload": payload}  # 调用方续跑管线
