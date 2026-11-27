"""复合键约定（spec §8.1 / §11）。

- 条目在库内的唯一寻址是四元组 (user_source,user_id,dimension,topic)。
- 行键统一写作 "{dimension}_{topic}"：entries.id 与 sources.entry_id 同为该值，
  可直接 JOIN；conflict_with、pending_clarifications、复核 decision、澄清目标
  也用同一标记。
"""
# sqlite-vec entry_id 合成键分隔符（单元分隔符，业务字段不会出现）
VEC_SEP = "\x1f"


def entry_key(dimension: str, topic: str) -> str:
    """行键 {dimension}_{topic}：entries.id 与 sources.entry_id 共用。"""
    return f"{dimension}_{topic}"


def vec_key(user_source: str, user_id: str, dimension: str, topic: str) -> str:
    """sqlite-vec 行主键：四元组合成键。"""
    return VEC_SEP.join((user_source, user_id, dimension, topic))


def canonical_ids(ids) -> str:
    """澄清对规范化：集合语义（顺序无关）的 JSON 串，作为 pending 主键。"""
    import json
    return json.dumps(sorted(set(ids)), ensure_ascii=False)
