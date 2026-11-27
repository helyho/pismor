"""topic_aliases 自动补写：守卫集中在 plan_topic_alias，落盘由 flush 负责。"""
from pismor.core.taxonomy import Taxonomy


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
