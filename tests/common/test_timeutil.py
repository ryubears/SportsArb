"""
Tests for the shared time helpers.
"""

from common import timeutil


def test_iso_accepts_each_venue_format():
    assert timeutil.iso("2026-09-20 17:00:00+00") == "2026-09-20T17:00:00+00:00"
    assert timeutil.iso("2026-10-01T00:15:00Z") == "2026-10-01T00:15:00+00:00"
    assert timeutil.iso("2026-09-20T17:00:00") == "2026-09-20T17:00:00+00:00"


def test_iso_passes_through_missing_and_unparseable_values():
    assert timeutil.iso(None) is None
    assert timeutil.iso("") is None
    assert timeutil.iso("garbage") == "garbage"


def test_utc_minute_reads_seconds_since_1970_to_the_minute():
    assert timeutil.utc_minute(1790000000) == "2026-09-21 14:13 UTC"
    assert timeutil.utc_minute(timeutil.epoch("2026-10-21T14:13:59Z")) == "2026-10-21 14:13 UTC"


def test_at_seconds_is_the_iso_time_epoch_reads_back():
    assert timeutil.at_seconds(1790000000.25) == "2026-09-21T14:13:20.250000+00:00"
    assert timeutil.epoch(timeutil.at_seconds(1790000000.25)) == 1790000000.25


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


def test_written_date_reads_the_month_in_full_or_short():
    assert timeutil.written_date("October 4, 2026") == timeutil.written_date("Oct 4, 2026") == "2026-10-04"
    assert timeutil.written_date("September 4, 2026") == timeutil.written_date("Sept. 4 2026") == "2026-09-04"
    assert timeutil.written_date("Noon 4, 2026") is None


def test_season_from_date_splits_in_august():
    assert timeutil.season_from_date("2026-09-20") == 2027
    assert timeutil.season_from_date("2027-01-10") == 2027
    assert timeutil.season_from_date("2027-08-01") == 2028
    assert timeutil.season_from_date("2026-09-29", "mlb") == 2026     # Baseball's season ends in the year it starts.


def test_add_business_days_skips_weekends():
    assert timeutil.add_business_days("2026-09-21T12:00:00+00:00", 4) == "2026-09-25T12:00:00+00:00"   # Monday to Friday.
    assert timeutil.add_business_days("2026-09-24T12:00:00+00:00", 4) == "2026-09-30T12:00:00+00:00"   # Thursday to Wednesday.
    assert timeutil.add_business_days("2026-09-26T12:00:00+00:00", 1) == "2026-09-28T12:00:00+00:00"   # Saturday to Monday.
