def test_load_taxonomy(taxonomy):
    assert set(taxonomy.dimensions) == {"taboo", "identity", "preference", "fact", "context"}
    assert taxonomy.dimensions["taboo"].priority == 0
    assert "tool" in taxonomy.dimensions["taboo"].topics
    assert taxonomy.meta["auto_activate"] is True
    assert any(c["quote"].startswith("别再") for c in taxonomy.regression_cases)

def test_priority_order(taxonomy):
    # 运行时注入顺序唯一来源：priority 升序
    assert taxonomy.priority_order() == ("taboo", "identity", "preference", "fact", "context")

def test_missing_file_raises(tmp_path):
    import pytest
    from pismor.core.taxonomy import load_taxonomy
    with pytest.raises(FileNotFoundError):
        load_taxonomy(tmp_path / "nope.yaml")
