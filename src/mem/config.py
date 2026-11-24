from dataclasses import dataclass
from pathlib import Path
import yaml

@dataclass(frozen=True)
class LLMSettings:
    endpoint: str
    model: str
    api_key_env: str

@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool
    endpoint: str
    model: str
    api_key_env: str
    dimensions: int
    batch_size: int

@dataclass(frozen=True)
class Settings:
    db_path: Path
    taxonomy_path: Path
    export_root: Path
    llm: LLMSettings
    embedding: EmbeddingSettings

def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Settings(
        db_path=Path(raw["db_path"]),
        taxonomy_path=Path(raw["taxonomy_path"]),
        export_root=Path(raw.get("export_root", "./exports")),
        llm=LLMSettings(**raw["llm"]),
        embedding=EmbeddingSettings(**raw["embedding"]),
    )
