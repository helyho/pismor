"""topic_aliases 自动补写：守卫集中在 plan_topic_alias，落盘由 flush 负责。"""
from pismor.core.config_files import (
    ConfigVersionConflict,
    dump_yaml,
    read_yaml,
    write_config,
)
from pismor.core.taxonomy import Taxonomy, load_taxonomy


def plan_topic_alias(
    taxonomy: Taxonomy, dim_key: str, alias: str, canonical: str
) -> dict | None:
    """把一个裸自由主题映射到同维度 canonical；不满足守卫返回 None。

    只增不改：已有映射（人工或自动）一律跳过；不写文件，只改内存并返回 append 项。
    """
    alias = (alias or "").strip()
    canonical = (canonical or "").strip()
    if not alias or not canonical:
        return None
    dim = taxonomy.dimensions.get(dim_key)
    if dim is None:
        return None
    if alias == canonical:
        return None
    disabled = dim.disabled_topics or []
    if alias in dim.topics or alias in disabled:
        return None
    if canonical not in dim.topics or canonical in disabled:
        return None
    if alias in dim.topic_aliases:
        return None
    dim.topic_aliases[alias] = canonical
    return {"dim": dim_key, "aliases": {alias: canonical}}


def flush_taxonomy_edits(
    taxonomy: Taxonomy,
    appends: list[dict],
    *,
    backup_root=None,
    keep: int = 30,
    label: str = "taxonomy-edit",
) -> Taxonomy | None:
    """事务提交成功后 round-trip 写回（保留注释），返回新 Taxonomy；无追加返回 None。"""
    if not appends or taxonomy.path is None:
        return None
    for attempt in range(2):
        data, _text, version = read_yaml(taxonomy.path)
        for a in appends:
            for d in data.get("dimensions", []):
                if d.get("key") != a.get("dim"):
                    continue
                topics = d.get("topics")
                if topics is None:
                    d["topics"] = topics = []
                for topic in a.get("topics") or []:
                    if topic not in topics:
                        topics.append(topic)
                aliases = d.get("topic_aliases")
                if aliases is None and a.get("aliases"):
                    d["topic_aliases"] = aliases = {}
                for alias, canonical in (a.get("aliases") or {}).items():
                    aliases.setdefault(alias, canonical)
            if a.get("case"):
                data.setdefault("regression_cases", []).append(a["case"])
        try:
            write_config(
                taxonomy.path,
                dump_yaml(data),
                expected_version=version,
                backup_root=backup_root,
                keep=keep,
                label=label,
            )
            return load_taxonomy(taxonomy.path)
        except ConfigVersionConflict:
            if attempt == 1:
                raise
    return None


def revert_taxonomy_edits(taxonomy: Taxonomy, appends: list[dict]) -> None:
    """事务回滚时撤销本轮内存追加，避免内存词表领先于库与文件。"""
    for a in reversed(appends):
        dim = taxonomy.dimensions.get(a.get("dim"))
        if dim is None:
            continue
        for alias in a.get("aliases") or {}:
            dim.topic_aliases.pop(alias, None)
        for topic in a.get("topics") or []:
            if topic in dim.topics:
                dim.topics.remove(topic)
