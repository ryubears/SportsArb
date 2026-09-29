"""
Tests for the latency report's reading of the log and of the venues' answers.
"""

import json
from datetime import datetime, timezone
from tools import latency_report

NOW = datetime(2026, 9, 29, 0, 5, tzinfo=timezone.utc)


def test_log_lines_get_their_dates_by_walking_back_over_midnight():
    lines = ["23:58:00 tracking a", "  a traceback line without a time", "23:59:00 tracking b", "00:01:00 tracking c"]
    assert [(when.isoformat(), line[-1]) for when, line in latency_report.dated(lines, NOW)] == [
        ("2026-09-28T23:58:00+00:00", "a"), ("2026-09-28T23:59:00+00:00", "b"), ("2026-09-29T00:01:00+00:00", "c")]


def test_status_lines_become_a_minute_of_changes_and_delays_each_and_a_restart_counts_over():
    def status(k, p, delays=True):
        extra = (", 12 ms behind the venue, 14 at 90%, 0 from us", ", 80 ms behind the venue, 150 at 90%, 1 from us") if delays else ("", "")
        return (f"tracking 5716 books, updates kalshi {k} (last 0s ago, 0 gaps{extra[0]}), "
                f"polymarket_us {p} (last 0s ago, 1 gaps{extra[1]})")
    lines = [(datetime(2026, 9, 29, 18, m, tzinfo=timezone.utc), f"18:0{m}:00 {text}") for m, text in
             [(0, status(1000, 400)), (1, status(4000, 1400)), (2, status(500, 200, delays=False))]]    # The last after a restart.
    rows = latency_report.status_minutes(lines)
    assert [r["kalshi"] for r in rows] == [(None, 12, 14, 0), (3000, 12, 14, 0), (500, None, None, None)]
    assert rows[1]["polymarket_us"] == (1000, 80, 150, 1)


def test_the_venue_time_of_an_order_comes_from_each_venues_answer():
    kalshi = json.dumps({"order_id": "o", "fill_count": "5.00", "ts_ms": 1790651367684})
    polymarket_us = json.dumps({"id": "CS", "executions": [{"type": "EXECUTION_TYPE_EXPIRED", "transactTime": "2026-09-29T00:53:53.085017Z",
                                                             "order": {"createTime": "2026-09-29T00:53:53.082786Z"}}]})
    assert latency_report.venue_time("kalshi", kalshi) == 1790651367.684
    assert latency_report.venue_time("polymarket_us", polymarket_us) == datetime(2026, 9, 29, 0, 53, 53, 85017, tzinfo=timezone.utc).timestamp()
    assert latency_report.venue_time("kalshi", json.dumps({"error": "refused"})) is None
    assert latency_report.venue_time("polymarket_us", "not json") is None
