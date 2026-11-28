from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
import typing
import yaml


def _rule(*, lo=None, hi=None, integer=False, restart=False, readonly=False, path=False):
    """字段规则元数据：校验边界与热更新行为的唯一声明处。

    config_service 从这里派生数值校验、重启分类、只读/路径集合，
    不再手写第二份字段清单。"""
    md = {}
    if lo is not None:
        md["min"] = lo
    if hi is not None:
        md["max"] = hi
    if integer:
        md["integer"] = True
    if restart:
        md["restart"] = True
    if readonly:
        md["readonly"] = True
    if path:
        md["path"] = True
    return md


@dataclass(frozen=True)
class LLMSettings:
    endpoint: str = field(metadata=_rule(restart=True))
    model: str = field(metadata=_rule(restart=True))
    api_key_env: str = field(metadata=_rule(restart=True))
    # 结构化输出（全字段 required）实测可超 30s，默认与代理对齐
    timeout_seconds: float = field(default=120.0, metadata=_rule(lo=0.1, restart=True))
    retries: int = field(default=1, metadata=_rule(lo=0, integer=True, restart=True))
    # transport 异常/429/5xx 的额外重试次数
    retry_backoff_seconds: float = field(
        default=0.5, metadata=_rule(lo=0, restart=True)
    )
    # 响应日志预览长度
    log_preview_chars: int = field(
        default=1000, metadata=_rule(lo=0, integer=True, restart=True)
    )
    # 配置后请求头注入 User-Agent（网关 UA 白名单场景）；默认 httpx 原生 UA
    user_agent: str | None = field(default=None, metadata=_rule(restart=True))
    # 结构化输出参数名；置空 "" 则不带该参数
    structured_output_param: str = field(
        default="response_format", metadata=_rule(restart=True)
    )
    # "json_schema"(OpenAI) | "json_object"(DeepSeek/GLM)
    structured_output_type: str = field(
        default="json_schema", metadata=_rule(restart=True)
    )


@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool = field(metadata=_rule(restart=True))
    endpoint: str = field(metadata=_rule(restart=True))
    model: str = field(metadata=_rule(restart=True))
    api_key_env: str = field(metadata=_rule(restart=True))
    dimensions: int = field(metadata=_rule(lo=1, integer=True, readonly=True))
    batch_size: int = field(metadata=_rule(lo=1, integer=True, restart=True))
    timeout_seconds: float = field(default=30.0, metadata=_rule(lo=0.1, restart=True))
    # 向量相关集最低 cosine 相似度；-1=不过滤
    min_similarity: float = field(default=0.5, metadata=_rule(lo=-1, hi=1))


@dataclass(frozen=True)
class GateSettings:
    # ≥此值且 explicit 直通入库
    auto_min: float = field(default=0.85, metadata=_rule(lo=0, hi=1))
    # ≥此值入复核队列，之下丢弃
    review_min: float = field(default=0.6, metadata=_rule(lo=0, hi=1))
    # inferred 候选置信压顶
    inferred_cap: float = field(default=0.8, metadata=_rule(lo=0, hi=1))


@dataclass(frozen=True)
class PipelineSettings:
    # 抽取时已有条目加载上限（提示词规模）
    known_ids_limit: int = field(default=200, metadata=_rule(lo=1, integer=True))
    # 无变化轮话题强化条数上限
    reinforce_top_k: int = field(default=20, metadata=_rule(lo=1, integer=True))
    # 消息长度突变阈值（字符）
    trigger_long_message: int = field(
        default=300, metadata=_rule(lo=1, integer=True)
    )
    # 候选 body 截断长度
    max_body_chars: int = field(default=200, metadata=_rule(lo=1, integer=True))
    # explicit 触发词表：默认覆盖中英文声明关键词；yaml 可整体替换或追加自定义词
    trigger_explicit_keywords: tuple[str, ...] = (
        "记住", "以后", "别再", "不要", "默认", "总是", "永远",
        "remember", "remember that", "from now on", "always", "never",
        "stop", "don't", "do not", "prefer", "keep in mind", "going forward",
    )


@dataclass(frozen=True)
class AggregatorSettings:
    # 无强化天数超过此值参与降级
    degrade_days: int = field(default=90, metadata=_rule(lo=0, integer=True))
    # 置信低于此值参与降级
    degrade_confidence: float = field(default=0.7, metadata=_rule(lo=0, hi=1))


@dataclass(frozen=True)
class AssemblerSettings:
    # merge_task 时 taboo 预算占比
    taboo_budget_share: float = field(default=0.6, metadata=_rule(lo=0, hi=1))
    # taboo 存活条数超过此值触发归并任务（装配与日批同源）
    taboo_merge_threshold: int = field(default=40, metadata=_rule(lo=1, integer=True))
    # BM25 词法索引分词器："jieba" | "bigram"（切换需 rebuild）
    tokenizer: str = field(default="jieba", metadata=_rule(restart=True))
    # 相关集召回条数
    related_top_k: int = field(default=20, metadata=_rule(lo=1, integer=True))
    # 强化时效半衰期
    half_life_days: float = field(default=30.0, metadata=_rule(lo=0.1))
    # 新建澄清 pending 的 TTL 默认
    clarify_ttl_days: int = field(default=7, metadata=_rule(lo=0, integer=True))
    # 召回未收尾冲突行（旧行为 A/B 开关；默认只注入无争议行）
    recall_conflicts: bool = False


@dataclass(frozen=True)
class IntakeSettings:
    # text 事件 content 上限
    max_content_chars: int = field(default=8192, metadata=_rule(lo=1, integer=True))
    # conversation 事件消息条数上限
    max_messages: int = field(default=100, metadata=_rule(lo=1, integer=True))
    # 单条消息 text 上限
    max_message_chars: int = field(default=8192, metadata=_rule(lo=1, integer=True))
    # 全部消息 text 总长上限
    max_total_chars: int = field(default=65536, metadata=_rule(lo=1, integer=True))


@dataclass(frozen=True)
class BackupSettings:
    # 快照滚动保留份数
    keep: int = field(default=30, metadata=_rule(lo=0, integer=True))
    # 备份根目录：db/ 与 config/ 两个子目录
    root: Path = field(default=Path("./backups"), metadata=_rule(path=True))


@dataclass(frozen=True)
class ServeSettings:
    # 日批循环间隔
    daily_interval_seconds: float = field(default=86400.0, metadata=_rule(lo=1))
    # 单个 HTTP 请求体上限（1 MiB）
    max_request_bytes: int = field(
        default=1_048_576, metadata=_rule(lo=1024, integer=True)
    )


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(metadata=_rule(readonly=True, path=True, restart=True))
    taxonomy_path: Path = field(metadata=_rule(readonly=True, path=True, restart=True))
    llm: LLMSettings = field(metadata=_rule())
    embedding: EmbeddingSettings = field(metadata=_rule())
    gate: GateSettings = field(default_factory=GateSettings, metadata=_rule())
    pipeline: PipelineSettings = field(default_factory=PipelineSettings, metadata=_rule())
    aggregator: AggregatorSettings = field(
        default_factory=AggregatorSettings, metadata=_rule()
    )
    assembler: AssemblerSettings = field(
        default_factory=AssemblerSettings, metadata=_rule()
    )
    intake: IntakeSettings = field(default_factory=IntakeSettings, metadata=_rule())
    backup: BackupSettings = field(default_factory=BackupSettings, metadata=_rule())
    serve: ServeSettings = field(default_factory=ServeSettings, metadata=_rule())
    # 管理后台读写 settings.yaml 的路径；直接构造时可为空
    config_path: Path | None = None


_SECTION_CLASSES = {
    f.name: f.type for f in fields(Settings) if is_dataclass(f.type)
}
_TOP_LEVEL_KEYS = {"db_path", "taxonomy_path", *_SECTION_CLASSES}


def _iter_field_rules():
    """遍历所有可配置字段路径（section.field），产出 (path, metadata)。"""
    for f in fields(Settings):
        if f.name == "config_path":
            continue
        section = f.type
        if is_dataclass(section):
            for sf in fields(section):
                yield f"{f.name}.{sf.name}", sf.metadata
        else:
            yield f.name, f.metadata


def numeric_rules() -> list[tuple[str, dict]]:
    """有数值边界的字段清单；config_service 据此生成校验，不再手写路径。"""
    return [
        (path, md)
        for path, md in _iter_field_rules()
        if "min" in md or "max" in md or md.get("integer")
    ]


def restart_fields() -> set[str]:
    return {path for path, md in _iter_field_rules() if md.get("restart")}


def readonly_fields() -> set[str]:
    return {path for path, md in _iter_field_rules() if md.get("readonly")}


def path_fields() -> set[str]:
    return {path for path, md in _iter_field_rules() if md.get("path")}


def _section(raw: dict, name: str, cls, unknown: list[str] | None = None):
    """yaml 段缺省时用 dataclass 全默认值；段内字段缺省时用各自默认（向后兼容旧配置）。

    yaml 列表自动转 tuple 注解字段的 tuple，保证与默认值类型一致（如词表）；
    Path 注解字段自动转 Path；未知字段被过滤，但原文件由 round-trip 保留，
    并把路径收集进 unknown 供配置校验提示（避免拼错字段静默失效）。"""
    kwargs = dict(raw.get(name) or {})
    known = set()
    for f in fields(cls):
        known.add(f.name)
        v = kwargs.get(f.name)
        if isinstance(v, list) and typing.get_origin(f.type) is tuple:
            kwargs[f.name] = tuple(v)
        if f.type is Path and isinstance(v, str):
            kwargs[f.name] = Path(v)
    if unknown is not None:
        unknown.extend(f"{name}.{k}" for k in kwargs if k not in known)
    return cls(**{k: v for k, v in kwargs.items() if k in known})


def parse_settings_strict(
    raw: dict, path: Path | None = None
) -> tuple[Settings, list[str]]:
    """解析并返回未知字段路径；未知字段不进入 Settings（旧配置兼容）。"""
    if not isinstance(raw, dict):
        raise ValueError("settings 顶层必须是 mapping")
    unknown = [str(k) for k in raw if k not in _TOP_LEVEL_KEYS]
    sections = {
        name: _section(raw, name, cls, unknown)
        for name, cls in _SECTION_CLASSES.items()
    }
    settings = Settings(
        db_path=Path(raw["db_path"]),
        taxonomy_path=Path(raw["taxonomy_path"]),
        config_path=path,
        **sections,
    )
    return settings, unknown


def parse_settings(raw: dict, path: Path | None = None) -> Settings:
    """把 YAML 原始 dict 转成 Settings；不读文件，供配置中心校验/保存复用。"""
    settings, _unknown = parse_settings_strict(raw, path)
    return settings


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parse_settings(raw, path=path)
