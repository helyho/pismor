from dataclasses import dataclass
from pathlib import Path
import yaml

@dataclass(frozen=True)
class Dimension:
    key: str
    name: str
    file: str
    priority: int
    default_scope: str
    topics: list[str]
    guidance: str

@dataclass(frozen=True)
class Taxonomy:
    meta: dict
    dimensions: dict[str, Dimension]
    invariants: list[str]
    regression_cases: list[dict]

def load_taxonomy(path: Path) -> Taxonomy:
    if not path.exists():
        raise FileNotFoundError(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    dims = {}
    for d in raw["dimensions"]:
        dims[d["key"]] = Dimension(
            key=d["key"], name=d["name"], file=d["file"], priority=d["priority"],
            default_scope=d["default_scope"], topics=d["topics"], guidance=d["guidance"],
        )
    return Taxonomy(meta=raw.get("meta", {}), dimensions=dims,
                    invariants=raw.get("invariants", []), regression_cases=raw.get("regression_cases", []))
