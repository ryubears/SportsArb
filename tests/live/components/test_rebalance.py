"""
Tests for rebalancing paper money between the venues.
"""

from db import database
from db.models import Settlement, Trade
from live.components import balances, rebalance


def test_weekly_check_moves_the_excess_and_it_lands_after_four_business_days(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    cash.amounts = {"kalshi": 3000.0, "polymarket_us": 7000.0}     # 2000 above a 5000 average, past the 25 percent drift.
    logs = []
    r = rebalance.Rebalancer(conn, cash, logs.append)
    r.rebalance("2026-09-21T12:00:00+00:00")                        # A Monday, no check.
    assert database.load_transfers(conn) == []
    r.rebalance("2026-09-22T12:00:00+00:00")                        # Tuesday.
    (transfer,) = database.load_transfers(conn)
    assert (transfer.from_venue, transfer.to_venue, transfer.amount, transfer.reason) == ("polymarket_us", "kalshi", 2000, "drift")
    assert transfer.expected_at == "2026-09-28T12:00:00+00:00"     # Four business days, over the weekend.
    assert cash.amounts == {"kalshi": 3000.0, "polymarket_us": 5000.0}     # In transit, on neither venue.
    assert r.summary() == "transfers: 2,000$ polymarket_us to kalshi, due 2026-09-28"
    r.rebalance("2026-09-22T13:00:00+00:00")                        # Same Tuesday, one transfer at a time anyway.
    assert len(database.load_transfers(conn)) == 1
    r.receive("2026-09-28T11:00:00+00:00")
    assert cash["kalshi"] == 3000.0
    r.receive("2026-09-28T12:00:00+00:00")
    assert cash.amounts == {"kalshi": 5000.0, "polymarket_us": 5000.0}
    assert database.load_transfers(conn)[0].arrived_at == "2026-09-28T12:00:00+00:00"
    assert r.summary() is None
    assert [tuple(x) for x in conn.execute("SELECT venue, amount, reason FROM ledger ORDER BY id")][2:] == [
        ("polymarket_us", -2000.0, "transfer_out"), ("kalshi", 2000.0, "transfer_in")]              # After the two openings.


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
    r.rebalance("2026-09-22T12:00:00+00:00")
    assert database.load_transfers(conn) == []


def open_trade(conn):
    """
    A filled trade whose contracts have not settled yet.
    """
    conn.execute("INSERT INTO pairs (label, kind, venues, contracts, flags, matched_at) VALUES ('p', 'game_winner', '', 2, '[]', 'm')")
    t = Trade(pair_id=1, trade="t", signal_ts="2026-09-22T00:30:00+00:00", edge=0.05, quantity=5, yes_venue="polymarket_us",
              yes_contract="pm", yes_polarity="yes", yes_limit=0.45, no_venue="kalshi", no_contract="k", no_polarity="yes",
              no_limit=0.47, pays_at="2026-09-22T04:15:00+00:00", yes_held=5, no_held=5, status="filled")
    database.insert_trade(conn, t)
    return t


def test_the_tuesday_check_waits_for_monday_nights_trades_to_settle(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    cash.amounts = {"kalshi": 3000.0, "polymarket_us": 7000.0}
    r = rebalance.Rebalancer(conn, cash, lambda m: None)
    t = open_trade(conn)                                            # Monday night's game, still out after midnight UTC.
    r.rebalance("2026-09-22T01:00:00+00:00")
    assert database.load_transfers(conn) == []
    database.insert_settlement(conn, Settlement(t.id, "2026-09-22T04:20:00+00:00"))
    r.rebalance("2026-09-22T04:30:00+00:00")                        # Still Tuesday, and nothing is open now.
    assert [x.reason for x in database.load_transfers(conn)] == ["drift"]


def test_a_venue_under_the_floor_waits_until_no_trade_is_open(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    cash.amounts = {"kalshi": 400.0, "polymarket_us": 6000.0}
    r = rebalance.Rebalancer(conn, cash, lambda m: None)
    t = open_trade(conn)
    r.rebalance("2026-09-23T12:00:00+00:00")                        # Its payout may be what brings the venue back.
    assert database.load_transfers(conn) == []
    database.insert_settlement(conn, Settlement(t.id, "2026-09-23T12:30:00+00:00"))
    r.rebalance("2026-09-23T13:00:00+00:00")
    assert [(x.reason, x.amount) for x in database.load_transfers(conn)] == [("floor", 2800)]
