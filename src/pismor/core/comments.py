"""从 round-trip YAML 中提取字段说明注释。

约定：只有与字段同一行的单行注释才算字段说明；段头块注释、上一字段后的注释
不挂到当前字段，避免 tooltip 显示成上一段内容。`dimensions[]` 归一化为
`dimensions[].field`，只取第一个维度的注释作为模板；逐条用例/主题列表的注释
不归一化。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from pismor.core.config_files import read_yaml


def _single_line_comment(token: Any) -> str | None:
    if token is None:
        return None
    if isinstance(token, list):
        for item in token:
            text = _single_line_comment(item)
            if text:
                return text
        return None
    value = token if isinstance(token, str) else getattr(token, "value", None)
    if not isinstance(value, str):
        return None
    # ruamel 的行内注释 token 通常带一个行尾换行；先去掉行尾，再判断是否跨行。
    value = value.rstrip("\r\n")
    if "\n" in value or "\r" in value:
        return None
    text = value.strip()
    if not text.startswith("#"):
        return None
    text = text.lstrip("#").strip()
    return text or None


def _same_line(item: Any, key_line: int | None) -> list[Any]:
    if not isinstance(item, list):
        return []
    tokens: list[Any] = []
    for part in item:
        if isinstance(part, list):
            tokens.extend(part)
        elif part is not None:
            tokens.append(part)
    if key_line is None:
        return tokens
    return [
        token
        for token in tokens
        if getattr(token, "start_mark", None) is not None
        and token.start_mark.line == key_line
    ]


def _mapping_comment(node: CommentedMap, key: Any) -> str | None:
    key_line = None
    try:
        key_line = node.lc.key(key)[0]
    except Exception:
        pass
    item = node.ca.items.get(key)
    # 块标量 `guidance: |  # 说明` 的注释落在 item[3]，ruamel 存成字符串列表。
    if isinstance(item, list) and len(item) > 3 and item[3]:
        for part in item[3]:
            text = _single_line_comment(part)
            if text:
                return text
    for token in _same_line(item, key_line):
        text = _single_line_comment(token)
        if text:
            return text
    return None


def collect_comments_from_data(data: Any) -> dict[str, str]:
    comments: dict[str, str] = {}

    def join(path: str, key: str) -> str:
        return f"{path}.{key}" if path else key

    def walk(node: Any, path: str) -> None:
        if isinstance(node, CommentedMap):
            for key, value in node.items():
                key_text = str(key)
                text = _mapping_comment(node, key)
                if text:
                    comments.setdefault(join(path, key_text), text)
                walk(value, join(path, key_text))
            return
        if isinstance(node, CommentedSeq):
            # 维度列表是模板：所有维度共用 dimensions[].field，先到先得。
            if path.endswith("dimensions"):
                for item in node:
                    walk(item, f"{path}[]")
            return

    walk(data, "")
    return comments


def collect_comments(path: Path) -> dict[str, str]:
    data, _text, _version = read_yaml(path)
    return collect_comments_from_data(data)
