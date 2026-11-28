from pismor.core import conflict_marks as cm


def test_decode_empty_and_valid():
    assert cm.decode(None) == []
    assert cm.decode("") == []
    assert cm.decode("[]") == []
    assert cm.decode('["a","b"]') == ["a", "b"]
    assert cm.decode("[1,2]") == ["1", "2"]


def test_decode_unparseable_is_none_but_or_empty_is_list():
    for bad in ("{bad", '{"a": 1}', '"a"', "null"):
        assert cm.decode(bad) is None
        assert cm.decode_or_empty(bad) == []


def test_encode_sorts_dedupes_and_none_for_empty():
    assert cm.encode([]) is None
    assert cm.encode(["b", "a", "a"]) == '["a", "b"]'
    assert cm.encode(("b", 1)) == '["1", "b"]'


def test_merge_keeps_existing_and_overrides_bad_json():
    assert cm.merge(None, "a") == '["a"]'
    assert cm.merge('["a"]', "b") == '["a", "b"]'
    assert cm.merge('["a"]', "a") == '["a"]'
    assert cm.merge("{bad", "b") == '["b"]'


def test_remove_and_empty_becomes_none():
    assert cm.remove('["a","b"]', ["a"]) == '["b"]'
    assert cm.remove('["a"]', ["a"]) is None
    assert cm.remove('["a"]', ["b"]) == '["a"]'
    assert cm.remove(None, ["a"]) is None


def test_remove_and_rekey_leave_bad_json_untouched():
    assert cm.remove("{bad", ["a"]) == "{bad"
    assert cm.rekey("{bad", "a", "c") == "{bad"


def test_rekey_rewrites_and_normalizes():
    assert cm.rekey('["a","b"]', "a", "c") == '["b", "c"]'
    assert cm.rekey('["b","b"]', "b", "d") == '["d"]'
    assert cm.rekey('["a"]', "x", "y") == '["a"]'


def test_contains():
    assert cm.contains('["a"]', "a")
    assert not cm.contains('["a"]', "b")
    assert not cm.contains("{bad", "a")
    assert not cm.contains(None, "a")
