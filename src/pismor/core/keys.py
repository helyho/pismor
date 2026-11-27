"""行键与变体约定（variant 重构后）。

- entries.id 是与业务字段解耦的稳定行键（uuid4 hex），改键/合并不再迁移 id。
- 条目寻址：内部一律用 id；(user_source,user_id,dimension,topic,variant,variant_seq)
  是唯一约束，用于同主题多形态共存。
- display_key 仅用于展示、日志、抽取提示词（known_ids），非唯一，不得写入
  sources.entry_id / conflict_with / pending_clarifications 等引用字段。
"""
import json
import uuid

# sqlite-vec 旧版四元组合成键分隔符；variant 重构后 embeddings.entry_id 直接存 entries.id。
VEC_SEP = "\x1f"

VARIANT_NORMAL = "normal"
VARIANT_CONFLICT = "conflict"
VARIANT_TRACE = "trace"
VARIANT_SNAPSHOT = "snapshot"
VARIANTS = (VARIANT_NORMAL, VARIANT_CONFLICT, VARIANT_TRACE, VARIANT_SNAPSHOT)


def new_id() -> str:
    """稳定行键：32 字符 uuid4 hex。写 entries / sources / conflict_with 一律用它。"""
    return uuid.uuid4().hex


def display_key(dimension: str, topic: str, variant: str = VARIANT_NORMAL,
                seq: int = 0) -> str:
    """人类可读键，仅展示/提示词用；非唯一。

    形态与旧行键对齐（`dimension_topic`、`+topic`、`~topic`、`!topic`），
    方便管理端和抽取提示词沿用旧观感；不要用它做数据库引用。
    """
    if variant == VARIANT_CONFLICT:
        suffix = f":{seq}" if seq > 1 else ""
        return f"{dimension}_+{topic}{suffix}"
    if variant == VARIANT_TRACE:
        return f"{dimension}_~{topic}"
    if variant == VARIANT_SNAPSHOT:
        suffix = f":{seq}" if seq > 1 else ""
        return f"{dimension}_!{topic}{suffix}"
    return f"{dimension}_{topic}"


def entry_key(dimension: str, topic: str) -> str:
    """兼容别名：旧调用方理解为“行键”，实际只是 display_key，非唯一。"""
    return display_key(dimension, topic)


def vec_key(user_source: str, user_id: str, dimension: str, topic: str) -> str:
    """已弃用：embeddings.entry_id 直接存 entries.id。

    保留函数仅供旧测试/迁移参考，新代码不要使用。
    """
    return VEC_SEP.join((user_source, user_id, dimension, topic))


def canonical_ids(ids) -> str:
    """澄清对规范化：集合语义（顺序无关）的 JSON 串，作为 pending 主键。"""
    return json.dumps(sorted(set(ids)), ensure_ascii=False)
