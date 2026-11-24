"""复合键与作用域约定（spec §8.1 / §11）。

- 条目在库内的唯一寻址是五元组 (user_source,user_id,dimension,topic,scope)；
  展示用 id 不含 scope，跨 scope 同 id 合法。任何跨行关联（conflict_with、
  pending_clarifications、复核 verdict、澄清目标）都必须额外携带 scope。
- 对外/持久化的关联标记写成 "{id}@{scope}"（如 preference_style@global）。
  旧数据里的裸 id 仍可解析：按所在行的 scope 补全（历史冲突对恒为同 scope）。
"""
import re

# 作用域白名单：global 或 project:<name>；name 限字母数字下划线中文连字符
SCOPE_RE = re.compile(r"^(global|project:[\w\u4e00-\u9fa5-]+)$")
_MARK_SEP = "@"
# sqlite-vec entry_id 合成键分隔符（单元分隔符，业务字段不会出现）
VEC_SEP = "\x1f"


def valid_scope(scope: str | None) -> bool:
    return bool(scope) and SCOPE_RE.fullmatch(scope) is not None


def mark(entry_id: str, scope: str) -> str:
    """条目关联标记 {id}@{scope}。"""
    return f"{entry_id}{_MARK_SEP}{scope}"


def split_mark(value: str, default_scope: str | None = None) -> tuple[str, str | None]:
    """解析关联标记；裸 id（旧数据）用 default_scope 补全。"""
    if _MARK_SEP in value:
        entry_id, scope = value.rsplit(_MARK_SEP, 1)
        return entry_id, scope
    return value, default_scope


def mark_matches(value: str, entry_id: str, scope: str, default_scope: str | None = None) -> bool:
    """标记是否精确定位 (entry_id, scope)；裸 id 仅在默认 scope 相等时命中。"""
    mid, mscope = split_mark(value, default_scope)
    return mid == entry_id and mscope == scope


def vec_key(user_source: str, user_id: str, dimension: str, topic: str, scope: str) -> str:
    """sqlite-vec 行主键：五元组合成键，避免同 id 跨 scope 向量互相覆盖。"""
    return VEC_SEP.join((user_source, user_id, dimension, topic, scope))


def canonical_ids(ids) -> str:
    """澄清对规范化：集合语义（顺序无关）的 JSON 串，作为 pending 主键。"""
    import json
    return json.dumps(sorted(set(ids)), ensure_ascii=False)
