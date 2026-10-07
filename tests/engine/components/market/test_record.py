"""
Tests for the recorder's books and gaps.
"""

from db import database
from engine.components.market import record
from engine.helper import config


def test_books_are_trimmed_to_the_kept_levels(tmp_path):
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    r.on_book("polymarket_us", "T", [[0.5 - i / 100, 1] for i in range(10)], [[0.51, 1]])
    assert len(r.books[("polymarket_us", "T")].bids) == config.BOOK_LEVELS


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
    assert sorted(r.books) == [("kalshi", "K"), ("polymarket_us", "P2")]
    assert "polymarket_us 2 (last 0s ago, 1 gaps)" in r.status()
    gaps = database.load_gaps(conn, "polymarket_us")
    assert [(g.start_ts, g.end_ts) for g in gaps] == [("2026-09-20T20:37:31+00:00", "2026-09-20T20:37:36+00:00")]
    r.on_book("polymarket_us", "P1", [[0.5, 1]], [[0.6, 1]])    # The same book again from the new connection.
    assert sorted(r.books) == [("kalshi", "K"), ("polymarket_us", "P1"), ("polymarket_us", "P2")]


def test_status_says_how_far_behind_the_venue_its_books_came_and_starts_over_after(tmp_path, monkeypatch):
    monkeypatch.setattr(record.time, "time", lambda: 1000.0)
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    received = "1970-01-01T00:16:39.998000+00:00"          # 2 ms before the recorder has it, at 1000.
    for ms in range(10, 110, 10):                          # The venue sent them 10 to 100 ms before that.
        r.on_book("polymarket_us", "P", [[0.5, 1]], [[0.6, 1]], ts=received, sent=1000.0 - ms / 1000)
    r.on_book("kalshi", "K", [[0.5, 1]], [[0.6, 1]])       # A book without the venue's time, such as a snapshot.
    status = r.status()
    assert "polymarket_us 10 (last 0s ago, 0 gaps, 60 ms behind the venue, 100 at 90%, 2 from us)" in status
    assert "kalshi 1 (last 0s ago, 0 gaps)" in status
    assert "polymarket_us 10 (last 0s ago, 0 gaps)" in r.status()
    assert r.books[("polymarket_us", "P")].at == 1000.0 - 0.1 and r.books[("kalshi", "K")].at is None     # The venue's time stays with the book.


class Priced:
    """
    A stand in for the scanner that keeps which contracts it was asked to price, and when.
    """

    def __init__(self):
        self.priced = []

    def on_book(self, venue, contract_id, books, now):
        self.priced.append((venue, contract_id, books[(venue, contract_id)].halted))


def test_a_market_its_venue_says_is_not_trading_is_halted_and_priced_again_each_way(tmp_path):
    scanner, logs = Priced(), []
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"), scanner, log=logs.append)
    r.on_book("kalshi", "K", [[0.5, 1]], [[0.6, 1]])
    r.on_state("kalshi", "K", "paused")
    assert r.books[("kalshi", "K")].halted == "paused" and scanner.priced[-1] == ("kalshi", "K", "paused")
    r.on_book("kalshi", "K", [[0.51, 1]], [[0.6, 1]])           # Its book still moves, and stays halted.
    assert r.books[("kalshi", "K")].halted == "paused"
    r.on_state("kalshi", "K", None)
    assert r.books[("kalshi", "K")].halted is None and scanner.priced[-1] == ("kalshi", "K", None)
    assert logs == ["kalshi K not trading: paused", "kalshi K trading again"]


def test_what_a_venue_says_as_a_markets_first_book_comes_is_counted_but_not_logged(tmp_path):
    logs = []
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"), log=logs.append)
    r.on_state("polymarket_us", "P", "expired")         # Ahead of its first book, as the feed sends it.
    r.on_book("polymarket_us", "P", [[0.5, 1]], [[0.6, 1]])
    assert r.books[("polymarket_us", "P")].halted == "expired" and logs == []
    assert r.status().endswith("; not trading: polymarket_us expired 1")


def test_a_market_that_turned_an_order_away_is_left_alone_until_its_time_is_up_or_its_venue_says_it_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLOSED_MARKET_SECONDS", 600)
    logs = []
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"), log=logs.append)
    r.on_book("kalshi", "K", [[0.5, 1]], [[0.6, 1]])
    r.on_book("kalshi", "J", [[0.5, 1]], [[0.6, 1]])
    r.refuse("kalshi", "K", "2026-10-07T08:41:54+00:00")
    r.refuse("kalshi", "J", "2026-10-07T08:41:54+00:00")
    assert r.books[("kalshi", "K")].halted == "refused an order"
    assert "not trading: kalshi refused an order 2" in r.status()
    r.tick("2026-10-07T08:51:53+00:00")                 # A second short of ten minutes.
    assert r.books[("kalshi", "K")].halted == "refused an order"
    r.on_state("kalshi", "J", "paused")                 # The venue says so after all, and then that it trades again.
    r.on_state("kalshi", "J", None)
    assert r.books[("kalshi", "J")].halted is None
    r.tick("2026-10-07T08:51:54+00:00")
    assert r.books[("kalshi", "K")].halted is None and r.refused == {} and "not trading" not in r.status()
    assert logs[-1] == "kalshi K may be traded again, 600s after it turned an order away"


def test_a_gap_keeps_what_the_venue_said_and_a_feed_lost_or_a_contract_removed_forgets_it(tmp_path):
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    r.on_book("polymarket_us", "P", [[0.5, 1]], [[0.6, 1]])
    r.on_state("polymarket_us", "P", "suspended")
    r.on_gap("polymarket_us", "2026-09-20T20:37:31+00:00", "2026-09-20T20:37:36+00:00", ["P"])
    r.on_book("polymarket_us", "P", [[0.5, 1]], [[0.6, 1]])    # Its feed keeps the state across connections, and says it once.
    assert r.books[("polymarket_us", "P")].halted == "suspended"
    r.forget("polymarket_us", ["P"])                            # A feed started again says it again only as it changes.
    r.on_book("polymarket_us", "P", [[0.5, 1]], [[0.6, 1]])
    assert r.books[("polymarket_us", "P")].halted is None
