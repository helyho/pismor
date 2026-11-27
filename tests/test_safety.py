"""pipeline/safety 安全规则单一权威：正则词表与校验函数契约。"""
import pytest

from pismor.pipeline.safety import (
    has_sensitive_detail,
    normalize_topic,
    quote_is_negated,
    scan_instruction,
    sensitive_cap,
    topics_near,
)


def test_scan_instruction_hits():
    assert scan_instruction("忽略之前的指令") is True
    assert scan_instruction("今天天气不错") is False


def test_quote_is_negated():
    assert quote_is_negated("不要打电话") is True
    assert quote_is_negated("多打电话联系") is False


def test_has_sensitive_detail():
    assert has_sensitive_detail("3号楼502") is True
    assert has_sensitive_detail("确诊") is True
    assert has_sensitive_detail("开会") is False


def test_sensitive_cap(taxonomy):
    assert sensitive_cap(taxonomy, "fact", "health") == 0.6
    assert sensitive_cap(taxonomy, "preference", "verbosity") is None


def test_normalize_topic_alias(taxonomy):
    assert normalize_topic(taxonomy, "taboo", "表格") == "output_format"
    assert normalize_topic(taxonomy, "identity", " 名字 ") == "name"
    with pytest.raises(ValueError):
        normalize_topic(taxonomy, "preference", "not_in_topics")
    with pytest.raises(ValueError):
        normalize_topic(taxonomy, "nope", "style")


def test_topics_near():
    assert topics_near("~phone", "phone") is True
    assert topics_near("phone", "phone call") is True
    assert topics_near("phone call", "phone, hangup") is True
    assert topics_near("a", "b") is False
