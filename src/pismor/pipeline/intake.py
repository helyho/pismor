import json
import sqlite3
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError
from pismor.core.schemas import EventIn, resolve_event_id

adapter = TypeAdapter(EventIn)

class ContentTooLong(ValueError):
    """content 超过 max_content_chars 上限（api 层转 422）。"""


def intake_event(conn, event: dict, *, max_content_chars: int = 8192) -> tuple[bool, dict]:
    # 畸形输入抛 pydantic.ValidationError（校验先于落库，不写 events_seen）
    ev = adapter.validate_python(event)
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
