"""
Tests for the shared JSON helpers.
"""

import pytest
from common import jsonutil


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


def test_float_or_none():
    assert jsonutil.float_or_none("4.5") == 4.5
    assert jsonutil.float_or_none(3) == 3.0
    assert jsonutil.float_or_none(None) is None
    assert jsonutil.float_or_none("n/a") is None


def test_float_or_zero_reads_a_missing_value_as_zero_and_refuses_one_that_is_not_a_number():
    assert (jsonutil.float_or_zero("0.4650"), jsonutil.float_or_zero(3), jsonutil.float_or_zero(None), jsonutil.float_or_zero("")) == (0.465, 3.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        jsonutil.float_or_zero("n/a")
