"""`entries.conflict_with` 标记集合的唯一解析、序列化与改写入口。

存储形态：JSON 数组，元素是条目的裸 id（字符串）。
空集合一律序列化为 ``None``，不写 ``"[]"``——查询侧用
``conflict_with IS NOT NULL`` 判定"存在未裁决冲突"，写 ``"[]"``
会让它误判。

坏 JSON 的两种口径由两个函数分别表达：

- :func:`decode` 无法解析时返回 ``None``，调用方据此跳过、不覆盖损坏数据；
- :func:`decode_or_empty` 供展示与审计使用，坏数据按空集合处理。
"""
from __future__ import annotations

import json
from collections.abc import Iterable


def decode(raw: str | None) -> list[str] | None:
    """解析标记；空值返回 ``[]``，无法解析返回 ``None``（调用方应跳过）。"""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list):
        return None
    return [str(item) for item in items]


def decode_or_empty(raw: str | None) -> list[str]:
    """展示/审计口径：坏数据当空集合，不中断调用方。"""
    return decode(raw) or []


def encode(ids: Iterable[object] | None) -> str | None:
    """排序去重后序列化；空集合（含 None）返回 ``None``。"""
    out = sorted({str(item) for item in (ids or ())})
    return json.dumps(out, ensure_ascii=False) if out else None


def merge(raw: str | None, mark_id: str) -> str | None:
    """并入一个标记。坏数据按空集合处理，以新标记为准。"""
    return encode([*decode_or_empty(raw), mark_id])


def remove(raw: str | None, ids: Iterable[object]) -> str | None:
    """删除若干标记；无法解析时原样返回，调用方比较后跳过。"""
    marks = decode(raw)
    if marks is None:
        return raw
    gone = {str(item) for item in ids}
    return encode(mark for mark in marks if mark not in gone)


def rekey(raw: str | None, old_id: str, new_id: str) -> str | None:
    """把 old_id 改写为 new_id；无法解析时原样返回，调用方比较后跳过。"""
    marks = decode(raw)
    if marks is None:
        return raw
    return encode(new_id if mark == str(old_id) else mark for mark in marks)


def contains(raw: str | None, entry_id: str) -> bool:
    return str(entry_id) in decode_or_empty(raw)
