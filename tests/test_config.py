"""配置项加载测试：yaml 全字段读取 + 段缺省回退默认值（向后兼容旧配置文件）。"""
import textwrap
from pathlib import Path

from pismor.core.config import load_settings

_FULL_YAML = textwrap.dedent("""
    db_path: ./data/index.db
    taxonomy_path: ./config/taxonomy.yaml
    llm:
      endpoint: "http://127.0.0.1:8399/v1"
      model: "glm5.3"
      api_key_env: LLM_API_KEY
      timeout_seconds: 60
      retries: 2
      log_preview_chars: 500
    embedding:
      enabled: false
      endpoint: ""
      model: ""
      api_key_env: ""
      dimensions: 4
      batch_size: 8
      timeout_seconds: 15
    gate:
      auto_min: 0.9
      review_min: 0.5
      inferred_cap: 0.75
    pipeline:
      known_ids_limit: 100
      reinforce_top_k: 10
      trigger_long_message: 500
      max_body_chars: 150
      trigger_explicit_keywords: ["记住", "remember", "prefer"]
    aggregator:
      degrade_days: 30
      degrade_confidence: 0.8
    assembler:
      taboo_budget_share: 0.5
      related_top_k: 5
      half_life_days: 7
      clarify_ttl_days: 3
    intake:
      max_content_chars: 1000
    backup:
      keep: 5
    serve:
      daily_interval_seconds: 3600
    """)

_MIN_YAML = textwrap.dedent("""
    db_path: ./data/index.db
    taxonomy_path: ./config/taxonomy.yaml
    llm:
      endpoint: "http://llm.test/v1"
      model: "m"
      api_key_env: K
    embedding:
      enabled: false
      endpoint: ""
      model: ""
      api_key_env: ""
      dimensions: 4
      batch_size: 8
    """)


def test_load_settings_reads_all_sections(tmp_path: Path):
    (tmp_path / "settings.yaml").write_text(_FULL_YAML, encoding="utf-8")
    s = load_settings(tmp_path / "settings.yaml")
    assert s.llm.timeout_seconds == 60 and s.llm.retries == 2 and s.llm.log_preview_chars == 500
    assert s.embedding.timeout_seconds == 15
    assert (s.gate.auto_min, s.gate.review_min, s.gate.inferred_cap) == (0.9, 0.5, 0.75)
    assert (s.pipeline.known_ids_limit, s.pipeline.reinforce_top_k) == (100, 10)
    assert (s.pipeline.trigger_long_message, s.pipeline.max_body_chars) == (500, 150)
    assert s.pipeline.trigger_explicit_keywords == ("记住", "remember", "prefer")
    assert (s.aggregator.degrade_days, s.aggregator.degrade_confidence) == (30, 0.8)
    assert (s.assembler.taboo_budget_share, s.assembler.related_top_k) == (0.5, 5)
    assert (s.assembler.half_life_days, s.assembler.clarify_ttl_days) == (7, 3)
    assert s.intake.max_content_chars == 1000
    assert s.backup.keep == 5 and s.serve.daily_interval_seconds == 3600


def test_load_settings_defaults_without_new_sections(tmp_path: Path):
    # 旧版配置文件（无新段）可加载：全部回退 dataclass 默认值
    (tmp_path / "settings.yaml").write_text(_MIN_YAML, encoding="utf-8")
    s = load_settings(tmp_path / "settings.yaml")
    assert s.llm.timeout_seconds == 120 and s.llm.retries == 1
    assert s.gate.auto_min == 0.85 and s.pipeline.known_ids_limit == 200
    assert "记住" in s.pipeline.trigger_explicit_keywords  # 默认词表含中文
    assert "remember" in s.pipeline.trigger_explicit_keywords  # 默认词表含英文
    assert s.aggregator.degrade_days == 90 and s.assembler.related_top_k == 20
    assert s.intake.max_content_chars == 8192 and s.backup.keep == 30
    assert s.serve.daily_interval_seconds == 86400.0
