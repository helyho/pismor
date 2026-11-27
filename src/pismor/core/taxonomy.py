from dataclasses import dataclass, field
from pathlib import Path
import yaml

@dataclass(frozen=True)
class Dimension:
    key: str
    name: str
    priority: int
    topics: list[str]
    guidance: str
    sensitive: bool | str = False
    topic_overrides: dict = None
    # 停用主题仍保留在 topics 中：存量条目继续可读，审计不因此隔离；
    # 抽取与 orphan 启用不再接收这些主题的新条目。
    disabled_topics: list[str] = field(default_factory=list)
    # 自由 topic → 白名单 topic 的归一别名：抽取时命中即改写为 canonical，
    # 避免同一语义拆成多个 topic 行（taboo 禁止运行期扩充，靠别名收敛碎片）。
    topic_aliases: dict = field(default_factory=dict)

@dataclass(frozen=True)
class Taxonomy:
    meta: dict
    dimensions: dict[str, Dimension]
    invariants: list[str]
    regression_cases: list[dict]
    path: Path | None = None  # 词表文件路径（aggregator 启用时写回 topics/regression_cases 用）

    def priority_order(self) -> tuple[str, ...]:
        """运行时维度顺序：priority 升序（配置校验保证不重复）。"""
        return tuple(d.key for d in sorted(self.dimensions.values(), key=lambda x: x.priority))

def load_taxonomy(path: Path) -> Taxonomy:
    if not path.exists():
        raise FileNotFoundError(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    dims = {}
    for d in raw["dimensions"]:
        dims[d["key"]] = Dimension(
            key=d["key"], name=d["name"], priority=d["priority"],
            topics=d["topics"], guidance=d["guidance"],
            sensitive=d.get("sensitive", False), topic_overrides=d.get("topic_overrides"),
            disabled_topics=list(d.get("disabled_topics") or []),
            topic_aliases=dict(d.get("topic_aliases") or {}),
        )
    return Taxonomy(meta=raw.get("meta", {}), dimensions=dims,
                    invariants=raw.get("invariants", []), regression_cases=raw.get("regression_cases", []),
                    path=path)
