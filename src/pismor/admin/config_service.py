"""配置中心的校验、差异与影响分析。

这里不写文件；路由负责写盘/备份/刷新 store。校验函数只接收 JSON 化后的
普通 dict，保证 YAML 源码模式与结构化表单走同一条校验链。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pismor.core.config import (
    Settings,
    numeric_rules,
    parse_settings_strict,
    path_fields,
    readonly_fields,
    restart_fields,
)

# 字段约束与分类只在 core.config 的 field metadata 声明一次，这里派生使用。
READONLY_SETTINGS = readonly_fields()
PATH_SETTINGS = path_fields()
UNUSED_TAXONOMY_META = {"id_pattern", "entry_language", "scenario"}


def _comparable(path: str, value: Any) -> Any:
    """路径字段按规范化后的 Path 比较，避免 ./data 与 data 被当成修改。"""
    if path in PATH_SETTINGS and isinstance(value, str) and value:
        return Path(value).expanduser()
    return value


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten(item, path))
    elif isinstance(value, list):
        out[prefix] = ", ".join("" if v is None else str(v) for v in value)
    else:
        out[prefix] = value
    return out


def diff_values(old: Any, new: Any) -> list[dict]:
    old_flat = flatten(old)
    new_flat = flatten(new)
    changes = []
    for path in sorted(set(old_flat) | set(new_flat)):
        before = old_flat.get(path)
        after = new_flat.get(path)
        if _comparable(path, before) != _comparable(path, after):
            changes.append({"path": path, "old": before, "new": after})
    return changes


def _num_errors(data: dict, path: str, *, lo=None, hi=None, integer=False) -> list[str]:
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            # 可选段/字段可整体省略，缺省由 dataclass 默认值补齐；必填字段由 parse_settings 兜底。
            return []
        value = value[part]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [f"{path}: 必须是数字"]
    if integer and int(value) != value:
        return [f"{path}: 必须是整数"]
    if lo is not None and value < lo:
        return [f"{path}: 不能小于 {lo}"]
    if hi is not None and value > hi:
        return [f"{path}: 不能大于 {hi}"]
    return []


def _settings_restart_paths(changes: list[dict]) -> list[str]:
    restart = restart_fields()
    return sorted(
        {
            change["path"]
            for change in changes
            if change["path"] in restart and change["path"] not in READONLY_SETTINGS
        }
    )


def validate_settings(
    raw: dict,
    current: Settings,
    *,
    config_path: Path | None = None,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "errors": ["settings 顶层必须是 mapping"],
            "warnings": [],
            "changes": [],
            "restart_required": [],
            "applied": [],
        }
    unknown: list[str] = []
    try:
        new_settings, unknown = parse_settings_strict(
            raw, path=config_path or current.config_path
        )
    except Exception as exc:  # noqa: BLE001 - 校验阶段把所有解析错误转成字段提示
        return {
            "ok": False,
            "errors": [f"配置解析失败: {exc}"],
            "warnings": [],
            "changes": [],
            "restart_required": [],
            "applied": [],
        }

    for path, rule in numeric_rules():
        errors += _num_errors(
            raw,
            path,
            lo=rule.get("min"),
            hi=rule.get("max"),
            integer=bool(rule.get("integer")),
        )

    for path in unknown:
        warnings.append(f"{path}: 未知配置项，已忽略（原文件中的该键会保留）")

    gate = raw.get("gate") or {}
    if (
        isinstance(gate.get("auto_min"), (int, float))
        and isinstance(gate.get("review_min"), (int, float))
        and gate["auto_min"] <= gate["review_min"]
    ):
        errors.append("gate.auto_min 必须大于 gate.review_min")

    keywords = (raw.get("pipeline") or {}).get("trigger_explicit_keywords")
    if keywords is not None:
        if not isinstance(keywords, list) or not keywords:
            errors.append("pipeline.trigger_explicit_keywords 必须是非空列表")
        elif any(not isinstance(item, str) or not item.strip() for item in keywords):
            errors.append("pipeline.trigger_explicit_keywords 不能包含空字符串")

    old_data = settings_to_data(current)
    changes = diff_values(old_data, raw)
    for change in changes:
        if change["path"] in READONLY_SETTINGS:
            errors.append(f"{change['path']}: 只读字段，不能在线修改")
    restart = _settings_restart_paths(changes)
    applied = [c["path"] for c in changes if c["path"] not in restart]

    taxonomy_path = new_settings.taxonomy_path
    if not taxonomy_path.exists():
        warnings.append(f"taxonomy_path 不存在: {taxonomy_path}")
    db_parent = new_settings.db_path.parent
    probe = db_parent if db_parent.exists() else db_parent.parent
    if probe.exists() and not os.access(probe, os.W_OK):
        warnings.append(f"db_path 目录不可写: {db_parent}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "changes": changes,
        "restart_required": restart,
        "applied": applied,
        "settings": settings_to_data(new_settings),
    }


def settings_to_data(settings: Settings) -> dict:
    def conv(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, tuple):
            return list(value)
        if hasattr(value, "__dataclass_fields__"):
            return {
                k: conv(getattr(value, k))
                for k in value.__dataclass_fields__
                if k != "config_path"
            }
        return value

    return conv(settings)


def validate_taxonomy(raw: dict, current, conn) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return {"ok": False, "errors": ["词表顶层必须是 mapping"], "warnings": [], "impact": []}

    dims = raw.get("dimensions")
    if not isinstance(dims, list) or not dims:
        errors.append("dimensions 必须是非空列表")
        dims = []

    keys: set[str] = set()
    priorities: set[int] = set()
    topics_by_dim: dict[str, set[str]] = {}
    disabled_by_dim: dict[str, set[str]] = {}
    for idx, dim in enumerate(dims):
        where = f"dimensions[{idx}]"
        if not isinstance(dim, dict):
            errors.append(f"{where}: 必须是 mapping")
            continue
        key = dim.get("key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{where}.key 不能为空")
            continue
        key = key.strip()
        if key in keys:
            errors.append(f"{where}.key 重复: {key}")
        keys.add(key)
        for field in ("name", "guidance"):
            if not isinstance(dim.get(field), str) or not dim[field].strip():
                errors.append(f"{where}.{field} 不能为空")
        priority = dim.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            errors.append(f"{where}.priority 必须是整数")
        elif priority in priorities:
            errors.append(f"{where}.priority 重复: {priority}")
        else:
            priorities.add(priority)
        topics = dim.get("topics")
        if not isinstance(topics, list) or not topics:
            errors.append(f"{where}.topics 必须是非空列表")
            topics = []
        clean_topics = []
        for topic in topics:
            if not isinstance(topic, str) or not topic.strip():
                errors.append(f"{where}.topics 不能包含空字符串")
                continue
            if topic in clean_topics:
                errors.append(f"{where}.topics 重复: {topic}")
            clean_topics.append(topic)
        topics_by_dim[key] = set(clean_topics)
        disabled = dim.get("disabled_topics")
        if disabled is None:
            disabled = []
        if not isinstance(disabled, list):
            errors.append(f"{where}.disabled_topics 必须是列表")
            disabled = []
        clean_disabled = []
        for topic in disabled:
            if not isinstance(topic, str) or not topic.strip():
                errors.append(f"{where}.disabled_topics 不能包含空字符串")
                continue
            if topic in clean_disabled:
                errors.append(f"{where}.disabled_topics 重复: {topic}")
            if topic not in clean_topics:
                errors.append(f"{where}.disabled_topics.{topic} 不在 topics 中；停用主题必须保留在 topics 内")
            clean_disabled.append(topic)
        disabled_by_dim[key] = set(clean_disabled)
        aliases = dim.get("topic_aliases")
        if aliases is None:
            aliases = {}
        if not isinstance(aliases, dict):
            errors.append(f"{where}.topic_aliases 必须是 mapping")
        else:
            for alias, canonical in aliases.items():
                if not isinstance(alias, str) or not alias.strip():
                    errors.append(f"{where}.topic_aliases 不能包含空别名")
                    continue
                if alias in clean_topics:
                    errors.append(
                        f"{where}.topic_aliases.{alias} 与 topics 中的主题同名；"
                        "别名会覆盖白名单主题原义，请改用其他别名"
                    )
                if not isinstance(canonical, str) or not canonical.strip():
                    errors.append(f"{where}.topic_aliases.{alias} 必须是非空字符串")
                elif canonical not in clean_topics:
                    errors.append(f"{where}.topic_aliases.{alias} 指向的 {canonical} 不在 topics 中")
                elif canonical in clean_disabled:
                    warnings.append(
                        f"{where}.topic_aliases.{alias} 指向已停用主题 {canonical}，别名会被丢弃"
                    )
        overrides = dim.get("topic_overrides") or {}
        if not isinstance(overrides, dict):
            errors.append(f"{where}.topic_overrides 必须是 mapping")
        else:
            for topic in overrides:
                if not isinstance(topic, str) or not topic.strip():
                    errors.append(f"{where}.topic_overrides 不能包含空主题")
                    continue
                if topic not in clean_topics:
                    errors.append(f"{where}.topic_overrides.{topic} 不在 topics 中")
                override = overrides[topic]
                if not isinstance(override, dict):
                    errors.append(f"{where}.topic_overrides.{topic} 必须是 mapping")
                    continue
                for field in sorted(set(override) - {"sensitive", "note"}):
                    warnings.append(
                        f"{where}.topic_overrides.{topic}.{field} 当前版本不参与运行逻辑，"
                        "只有 sensitive/note 生效"
                    )
                sensitive = override.get("sensitive")
                if (
                    sensitive is not None
                    and not isinstance(sensitive, bool)
                    and sensitive != "partial"
                ):
                    warnings.append(
                        f"{where}.topic_overrides.{topic}.sensitive 建议用布尔值或 partial；"
                        "其他真值会按敏感处理"
                    )
                note = override.get("note")
                if note is not None and not isinstance(note, str):
                    warnings.append(f"{where}.topic_overrides.{topic}.note 建议为字符串")

    cases = raw.get("regression_cases") or []
    if not isinstance(cases, list):
        errors.append("regression_cases 必须是列表")
        cases = []
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"regression_cases[{idx}] 必须是 mapping")
        elif not isinstance(case.get("quote"), str) or not case["quote"].strip():
            errors.append(f"regression_cases[{idx}].quote 不能为空")

    meta = raw.get("meta") or {}
    if not isinstance(meta, dict):
        errors.append("meta 必须是 mapping")
        meta = {}
    for field in sorted(UNUSED_TAXONOMY_META):
        if field in meta:
            warnings.append(f"meta.{field} 当前版本不参与运行逻辑，仅作说明")
    if "invariants" in raw:
        warnings.append("invariants 当前版本仅作文档性不变量，不参与运行逻辑")

    impact = _taxonomy_impact(current, keys, topics_by_dim, disabled_by_dim, conn)
    removed = [item for item in impact if item["removed"]]
    if removed:
        affected = {(item["dimension"], item["topic"]) for item in removed}
        errors.append(
            f"本次将硬删除 {len(affected)} 个维度/主题，影响 {sum(i['count'] for i in removed)} 条存量条目；"
            "被引用主题不能硬删除，请改为停用（disabled_topics）"
        )
    disabled = [item for item in impact if item.get("disabled")]
    if disabled:
        affected = {(item["dimension"], item["topic"]) for item in disabled}
        warnings.append(
            f"本次停用 {len(affected)} 个主题，{sum(i['count'] for i in disabled)} 条存量条目保留；"
            "停用只阻止新条目，审计不会因此隔离存量条目"
        )
    return {"ok": not errors, "errors": errors, "warnings": warnings, "impact": impact}


def _taxonomy_impact(
    current,
    new_keys: set[str],
    new_topics: dict[str, set[str]],
    new_disabled: dict[str, set[str]],
    conn,
) -> list[dict]:
    rows = conn.execute(
        "SELECT dimension, topic, status, COUNT(*) AS count FROM entries"
        " GROUP BY dimension, topic, status"
    ).fetchall()
    old_keys = set(current.dimensions)
    old_topics = {
        key: set(getattr(dim, "topics", None) or [])
        for key, dim in current.dimensions.items()
    }
    old_disabled = {
        key: set(getattr(dim, "disabled_topics", None) or [])
        for key, dim in current.dimensions.items()
    }
    impact = []
    for row in rows:
        dim, topic = row["dimension"], row["topic"]
        # 只统计当前词表内确实存在的主题被移除；
        # orphan 维度、~ 归档主题、+ 合成主题等词表外数据不算硬删除。
        if topic not in old_topics.get(dim, set()):
            continue
        removed = dim not in new_keys or topic not in new_topics.get(dim, set())
        if removed:
            impact.append(
                {
                    "dimension": dim,
                    "topic": topic,
                    "status": row["status"],
                    "count": row["count"],
                    "removed": True,
                    "dimension_removed": dim in old_keys and dim not in new_keys,
                }
            )
            continue
        if topic in new_disabled.get(dim, set()) - old_disabled.get(dim, set()):
            impact.append(
                {
                    "dimension": dim,
                    "topic": topic,
                    "status": row["status"],
                    "count": row["count"],
                    "removed": False,
                    "disabled": True,
                }
            )
    return impact
