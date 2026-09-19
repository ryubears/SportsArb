"""
Tests for the shared time helpers.
"""

from util import timeutil


def test_iso_accepts_each_venue_format():
    assert timeutil.iso("2026-09-20 17:00:00+00") == "2026-09-20T17:00:00+00:00"
    assert timeutil.iso("2026-10-01T00:15:00Z") == "2026-10-01T00:15:00+00:00"
    assert timeutil.iso("2026-09-20T17:00:00") == "2026-09-20T17:00:00+00:00"


def test_iso_passes_through_missing_and_unparseable_values():
    assert timeutil.iso(None) is None
    assert timeutil.iso("") is None
    assert timeutil.iso("garbage") == "garbage"


def test_shift_moves_forward_by_hours_and_days():
    t = "2026-09-20T17:00:00+00:00"
    assert timeutil.shift(t, hours=4) == "2026-09-20T21:00:00+00:00"
    assert timeutil.shift(t, days=7) == "2026-09-27T17:00:00+00:00"


def test_seconds_and_days_between():
    a, b = "2026-09-20T00:00:00+00:00", "2026-09-20T12:00:00+00:00"
    assert timeutil.seconds_between(a, b) == 43200
    assert timeutil.days_between(a, b) == 0.5
    assert timeutil.seconds_between(b, a) == -43200


def test_eastern_date_rolls_late_utc_games_back_a_day():
    # A Thursday night game at 8:15 PM Eastern is already Friday in UTC.
    assert timeutil.eastern_date("2026-10-23T00:15:00+00:00") == "2026-10-22"
    assert timeutil.eastern_date("2026-09-20T17:00:00+00:00") == "2026-09-20"
