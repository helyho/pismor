from dataclasses import dataclass, fields
from pathlib import Path
import typing
import yaml

@dataclass(frozen=True)
class LLMSettings:
    endpoint: str
    model: str
    api_key_env: str
    timeout_seconds: float = 120.0  # 结构化输出（全字段 required）实测可超 30s，默认与代理对齐
    retries: int = 1
    retry_backoff_seconds: float = 0.5                # transport 异常/429/5xx 的额外重试次数
    log_preview_chars: int = 1000    # 响应日志预览长度
    user_agent: str | None = None   # 配置后请求头注入 User-Agent（网关 UA 白名单场景）；默认 httpx 原生 UA
    structured_output_param: str = "response_format"  # 结构化输出参数名；置空 "" 则不带该参数
    structured_output_type: str = "json_schema"       # "json_schema"(OpenAI) | "json_object"(DeepSeek/GLM)

@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool
    endpoint: str
    model: str
    api_key_env: str
    dimensions: int
    batch_size: int
    timeout_seconds: float = 30.0
    min_similarity: float = 0.5  # 向量相关集最低 cosine 相似度；-1=不过滤

@dataclass(frozen=True)
class GateSettings:
    auto_min: float = 0.85        # ≥此值且 explicit 直通入库
    review_min: float = 0.6       # ≥此值入复核队列，之下丢弃
    inferred_cap: float = 0.8     # inferred 候选置信压顶

@dataclass(frozen=True)
class PipelineSettings:
    known_ids_limit: int = 200      # 抽取时已有条目加载上限（提示词规模）
    reinforce_top_k: int = 20       # 无变化轮话题强化条数上限
    trigger_long_message: int = 300 # 消息长度突变阈值（字符）
    max_body_chars: int = 200       # 候选 body 截断长度
    # explicit 触发词表：默认覆盖中英文声明关键词；yaml 可整体替换或追加自定义词
    trigger_explicit_keywords: tuple[str, ...] = (
        "记住", "以后", "别再", "不要", "默认", "总是", "永远",
        "remember", "remember that", "from now on", "always", "never",
        "stop", "don't", "do not", "prefer", "keep in mind", "going forward",
    )

@dataclass(frozen=True)
class AggregatorSettings:
    degrade_days: int = 90          # 无强化天数超过此值参与降级
    degrade_confidence: float = 0.7 # 置信低于此值参与降级

@dataclass(frozen=True)
class AssemblerSettings:
    taboo_budget_share: float = 0.6  # merge_task 时 taboo 预算占比
    taboo_merge_threshold: int = 40  # taboo 存活条数超过此值触发归并任务（装配与日批同源）
    tokenizer: str = "jieba"         # BM25 词法索引分词器："jieba" | "bigram"（切换需 rebuild）
    related_top_k: int = 20          # 相关集召回条数
    half_life_days: float = 30.0     # 强化时效半衰期
    clarify_ttl_days: int = 7        # 新建澄清 pending 的 TTL 默认
    recall_conflicts: bool = False   # 召回未收尾冲突行（旧行为 A/B 开关；默认只注入无争议行）

@dataclass(frozen=True)
class IntakeSettings:
    max_content_chars: int = 8192   # text 事件 content 上限
    max_messages: int = 100         # conversation 事件消息条数上限
    max_message_chars: int = 8192   # 单条消息 text 上限
    max_total_chars: int = 65536    # 全部消息 text 总长上限

@dataclass(frozen=True)
class BackupSettings:
    keep: int = 30                  # 快照滚动保留份数
    root: Path = Path("./backups")  # 备份根目录：db/ 与 config/ 两个子目录

@dataclass(frozen=True)
class ServeSettings:
    daily_interval_seconds: float = 86400.0  # 日批循环间隔
    max_request_bytes: int = 1_048_576       # 单个 HTTP 请求体上限（1 MiB）

@dataclass(frozen=True)
class Settings:
    db_path: Path
    taxonomy_path: Path
    llm: LLMSettings
    embedding: EmbeddingSettings
    gate: GateSettings = GateSettings()
    pipeline: PipelineSettings = PipelineSettings()
    aggregator: AggregatorSettings = AggregatorSettings()
    assembler: AssemblerSettings = AssemblerSettings()
    intake: IntakeSettings = IntakeSettings()
    backup: BackupSettings = BackupSettings()
    serve: ServeSettings = ServeSettings()
    config_path: Path | None = None  # 管理后台读写 settings.yaml 的路径；直接构造时可为空


def _section(raw: dict, name: str, cls):
    """yaml 段缺省时用 dataclass 全默认值；段内字段缺省时用各自默认（向后兼容旧配置）。

    yaml 列表自动转 tuple 注解字段的 tuple，保证与默认值类型一致（如词表）；
    Path 注解字段自动转 Path；未知字段被过滤，但原文件由 round-trip 保留。"""
    kwargs = dict(raw.get(name) or {})
    known = set()
    for f in fields(cls):
        known.add(f.name)
        v = kwargs.get(f.name)
        if isinstance(v, list) and typing.get_origin(f.type) is tuple:
            kwargs[f.name] = tuple(v)
        if f.type is Path and isinstance(v, str):
            kwargs[f.name] = Path(v)
    return cls(**{k: v for k, v in kwargs.items() if k in known})


def parse_settings(raw: dict, path: Path | None = None) -> Settings:
    """把 YAML 原始 dict 转成 Settings；不读文件，供配置中心校验/保存复用。"""
    if not isinstance(raw, dict):
        raise ValueError("settings 顶层必须是 mapping")
    return Settings(
        db_path=Path(raw["db_path"]),
        taxonomy_path=Path(raw["taxonomy_path"]),
        llm=_section(raw, "llm", LLMSettings),
        embedding=_section(raw, "embedding", EmbeddingSettings),
        gate=_section(raw, "gate", GateSettings),
        pipeline=_section(raw, "pipeline", PipelineSettings),
        aggregator=_section(raw, "aggregator", AggregatorSettings),
        assembler=_section(raw, "assembler", AssemblerSettings),
        intake=_section(raw, "intake", IntakeSettings),
        backup=_section(raw, "backup", BackupSettings),
        serve=_section(raw, "serve", ServeSettings),
        config_path=path,
    )


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parse_settings(raw, path=path)
