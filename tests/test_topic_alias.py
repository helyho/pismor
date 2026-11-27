"""topic_aliases 自动补写：守卫、round-trip、回滚与复核返回值。"""
import shutil
from pathlib import Path

import pytest

from pismor.core.config_files import (
    ConfigVersionConflict,
    dump_yaml,
    read_yaml,
    write_config,
)
from pismor.core.taxonomy import load_taxonomy
from pismor.core.taxonomy_edit import (
    flush_taxonomy_edits,
    plan_topic_alias,
    revert_taxonomy_edits,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def tax():
    return load_taxonomy(REPO / "config" / "taxonomy.yaml")


@pytest.fixture
def tax_writable(tmp_path):
    dst = tmp_path / "taxonomy.yaml"
    shutil.copy(REPO / "config" / "taxonomy.yaml", dst)
    return load_taxonomy(dst)


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


def test_flush_writes_alias_and_keeps_comments(tax_writable):
    dim = tax_writable.dimensions["preference"]
    append = plan_topic_alias(tax_writable, "preference", "沟通方式", dim.topics[0])
    before = tax_writable.path.read_text(encoding="utf-8")
    new = flush_taxonomy_edits(tax_writable, [append], label="taxonomy-alias")
    text = tax_writable.path.read_text(encoding="utf-8")
    assert "topic_aliases:" in text and "沟通方式" in text
    assert "停用主题" in text  # 注释仍在
    assert before != text
    assert new.dimensions["preference"].topic_aliases["沟通方式"] == dim.topics[0]


def test_flush_retries_version_conflict(tax_writable, monkeypatch):
    dim = tax_writable.dimensions["preference"]
    append = plan_topic_alias(tax_writable, "preference", "别称", dim.topics[0])
    real = write_config
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConfigVersionConflict("conflict")
        return real(*args, **kwargs)

    monkeypatch.setattr("pismor.core.taxonomy_edit.write_config", flaky)
    assert flush_taxonomy_edits(tax_writable, [append]) is not None
    assert calls["n"] == 2


def test_flush_does_not_overwrite_existing_alias(tax_writable):
    dim = tax_writable.dimensions["preference"]
    canon = dim.topics[0]
    append = plan_topic_alias(tax_writable, "preference", "别称", canon)
    # 模拟另一进程抢先写入同一 alias 的不同 canonical
    data, _text, version = read_yaml(tax_writable.path)
    for d in data["dimensions"]:
        if d["key"] == "preference":
            d.setdefault("topic_aliases", {})["别称"] = dim.topics[1]
    write_config(tax_writable.path, dump_yaml(data), expected_version=version)
    new = flush_taxonomy_edits(tax_writable, [append])
    assert new.dimensions["preference"].topic_aliases["别称"] == dim.topics[1]


def test_revert_removes_only_new_alias(tax_writable):
    dim = tax_writable.dimensions["preference"]
    dim.topic_aliases["既有"] = dim.topics[0]
    append = plan_topic_alias(tax_writable, "preference", "新别名", dim.topics[0])
    revert_taxonomy_edits(tax_writable, [append])
    assert "新别名" not in dim.topic_aliases
    assert dim.topic_aliases["既有"] == dim.topics[0]
