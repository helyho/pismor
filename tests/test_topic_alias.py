"""topic_aliases 自动补写：守卫、round-trip、回滚与复核返回值。"""
from pathlib import Path

import pytest

from pismor.core.taxonomy import load_taxonomy
from pismor.core.taxonomy_edit import plan_topic_alias

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def tax():
    return load_taxonomy(REPO / "config" / "taxonomy.yaml")


def test_plan_alias_happy_path(tax):
    dim = tax.dimensions["preference"]
    canon = dim.topics[0]
    assert plan_topic_alias(tax, "preference", "沟通方式", canon) == {
        "dim": "preference", "aliases": {"沟通方式": canon}}
    assert dim.topic_aliases["沟通方式"] == canon


@pytest.mark.parametrize("alias,canon", [
    ("  ", "style"), ("别称", "  "), ("别称", "not_in_topics"),
])
def test_plan_alias_rejects_invalid(tax, alias, canon):
    assert plan_topic_alias(tax, "preference", alias, canon) is None


def test_plan_alias_rejects_same_and_shadow(tax):
    dim = tax.dimensions["preference"]
    assert plan_topic_alias(tax, "preference", dim.topics[0], dim.topics[0]) is None
    assert plan_topic_alias(tax, "preference", dim.topics[0], dim.topics[1]) is None


def test_plan_alias_rejects_disabled(tax):
    dim = tax.dimensions["preference"]
    dim.disabled_topics.append(dim.topics[0])
    assert plan_topic_alias(tax, "preference", "别称", dim.topics[0]) is None
    dim.disabled_topics.clear()
    dim.topics.append("停用别名")
    dim.disabled_topics.append("停用别名")
    assert plan_topic_alias(tax, "preference", "停用别名", dim.topics[1]) is None


def test_plan_alias_never_overwrites(tax):
    dim = tax.dimensions["preference"]
    dim.topic_aliases["别称"] = dim.topics[0]
    assert plan_topic_alias(tax, "preference", "别称", dim.topics[1]) is None


def test_plan_alias_unknown_dimension(tax):
    assert plan_topic_alias(tax, "nope", "别称", "style") is None
