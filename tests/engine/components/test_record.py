"""
Tests for the recorder's books and gaps.
"""

from db import database
from engine.components import record
from engine.helper import config


def test_books_are_trimmed_to_the_kept_levels(tmp_path):
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    r.on_book("polymarket_us", "T", [[0.5 - i / 100, 1] for i in range(10)], [[0.51, 1]])
    assert len(r.latest[("polymarket_us", "T")].bids) == config.BOOK_LEVELS


def test_status_reports_time_since_each_venue_updated(tmp_path):
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    assert "last never" in r.status()
    r.on_book("kalshi", "T", [[0.5, 1]], [[0.6, 1]])
    assert "kalshi 1 (last 0s ago, 0 gaps)" in r.status()


def test_a_gap_is_stored_and_only_the_dropped_connections_books_wait_to_be_sent_again(tmp_path):
    conn = database.connect(tmp_path / "test.sqlite")
    r = record.Recorder(conn)
    r.on_book("polymarket_us", "P1", [[0.5, 1]], [[0.6, 1]])    # Carried by the connection that drops.
    r.on_book("polymarket_us", "P2", [[0.5, 1]], [[0.6, 1]])    # Carried by another Polymarket US connection.
    r.on_book("kalshi", "K", [[0.5, 1]], [[0.6, 1]])
    r.on_gap("polymarket_us", "2026-09-20T20:37:31+00:00", "2026-09-20T20:37:36+00:00", ["P1"])
    assert sorted(r.latest) == [("kalshi", "K"), ("polymarket_us", "P2")]
    assert "polymarket_us 2 (last 0s ago, 1 gaps)" in r.status()
    gaps = database.load_gaps(conn, "polymarket_us")
    assert [(g.start_ts, g.end_ts) for g in gaps] == [("2026-09-20T20:37:31+00:00", "2026-09-20T20:37:36+00:00")]
    r.on_book("polymarket_us", "P1", [[0.5, 1]], [[0.6, 1]])    # The same book again from the new connection.
    assert sorted(r.latest) == [("kalshi", "K"), ("polymarket_us", "P1"), ("polymarket_us", "P2")]
