"""
Tests for the shared JSON helpers.
"""

from util import jsonutil


def test_parse_returns_default_for_missing_text():
    assert jsonutil.parse(None, []) == []
    assert jsonutil.parse("", {}) == {}
    assert jsonutil.parse(None) is None


def test_parse_reads_json_text():
    assert jsonutil.parse('[1, 2]') == [1, 2]
    assert jsonutil.parse('{"a": 1}') == {"a": 1}


def test_dump_keeps_none_as_none():
    assert jsonutil.dump(None) is None
    assert jsonutil.dump({"a": 1}) == '{"a": 1}'
    assert jsonutil.dump([]) == "[]"


def test_read_file(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"team": "BUF"}')
    assert jsonutil.read_file(path) == {"team": "BUF"}
