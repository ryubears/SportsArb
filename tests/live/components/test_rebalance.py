"""
Tests for rebalancing paper money between the venues.
"""

from db import database
from live.components import balances, rebalance


def test_weekly_check_moves_the_excess_and_it_lands_after_four_business_days(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    cash.amounts = {"kalshi": 3000.0, "polymarket_us": 7000.0}     # 2000 above a 5000 average, past the 25 percent drift.
    logs = []
    r = rebalance.Rebalancer(conn, cash, logs.append)
    r.rebalance("2026-09-20T12:00:00+00:00")                        # A Sunday, no check.
    assert database.load_transfers(conn) == []
    r.rebalance("2026-09-21T12:00:00+00:00")                        # Monday.
    (transfer,) = database.load_transfers(conn)
    assert (transfer.from_venue, transfer.to_venue, transfer.amount, transfer.reason) == ("polymarket_us", "kalshi", 2000, "drift")
    assert transfer.expected_at == "2026-09-25T12:00:00+00:00"
    assert cash.amounts == {"kalshi": 3000.0, "polymarket_us": 5000.0}     # In transit, on neither venue.
    assert r.summary() == "transfers: 2,000$ polymarket_us to kalshi, due 2026-09-25"
    r.rebalance("2026-09-21T13:00:00+00:00")                        # Same Monday, one transfer at a time anyway.
    assert len(database.load_transfers(conn)) == 1
    r.receive("2026-09-25T11:00:00+00:00")
    assert cash["kalshi"] == 3000.0
    r.receive("2026-09-25T12:00:00+00:00")
    assert cash.amounts == {"kalshi": 5000.0, "polymarket_us": 5000.0}
    assert database.load_transfers(conn)[0].arrived_at == "2026-09-25T12:00:00+00:00"
    assert r.summary() is None
    assert [tuple(x) for x in conn.execute("SELECT venue, amount, reason FROM ledger ORDER BY id")] == [
        ("polymarket_us", -2000.0, "transfer_out"), ("kalshi", 2000.0, "transfer_in")]


def test_a_venue_under_the_floor_is_topped_up_on_any_day(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    cash.amounts = {"kalshi": 400.0, "polymarket_us": 6000.0}
    r = rebalance.Rebalancer(conn, cash, lambda m: None)
    r.rebalance("2026-09-23T12:00:00+00:00")                        # A Wednesday.
    (transfer,) = database.load_transfers(conn)
    assert (transfer.reason, transfer.amount) == ("floor", 2800)


def test_balanced_venues_need_no_transfer(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    cash.amounts = {"kalshi": 4600.0, "polymarket_us": 5400.0}     # 8 percent apart.
    r = rebalance.Rebalancer(conn, cash, lambda m: None)
    r.rebalance("2026-09-21T12:00:00+00:00")
    assert database.load_transfers(conn) == []
