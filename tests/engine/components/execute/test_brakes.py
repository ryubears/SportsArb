"""
Tests for when live trading halts, from trades, orders, and settlements stored by each test.
"""

import asyncio
import pytest
from db import database
from db.models import Alert, Order, Settlement, Trade
from engine.components.balance.live import LiveBalances
from engine.components.execute import brakes

NOW = "2026-09-27T20:00:00+00:00"


@pytest.fixture(autouse=True)
def halt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(brakes, "HALT_FILE", tmp_path / "live_halt.txt")
    return brakes.HALT_FILE


def setup(tmp_path, cash_each=50.0):
    """
    A database with one pair, and Brakes over live money of cash_each dollars read on each venue.
    """
    conn = database.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO pairs (id, label, kind, venues, contracts, flags, matched_at) VALUES (1, 'p', 'game_winner', '', 2, '[]', 'm')")
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: cash_each, "polymarket_us": lambda: cash_each})
    asyncio.run(cash.refresh(NOW))
    return conn, brakes.Brakes(conn, cash, lambda m: None, clock=lambda: NOW)


def live_trade(conn, yes_held, no_held, orders, settled=None, yes_cost=0.0, no_cost=0.0):
    """
    Store a done live trade holding yes_held and no_held, with its orders as [(action, dollars, answered at, status)],
    and a settlement as (settled at, payouts) when given.
    """
    t = Trade(mode="live", pair_id=1, trade="t", signal_ts="2026-09-27T17:30:00+00:00", edge=0.08, quantity=10,
              yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45,
              no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.47, pays_at="2026-09-27T21:00:00+00:00",
              yes_held=yes_held, no_held=no_held, yes_cost=yes_cost, no_cost=no_cost, status="filled")
    database.insert_trade(conn, t)
    for action, dollars, at, status in orders:
        database.insert_order(conn, Order(trade_id=t.id, venue="kalshi", contract_id="k", purpose="open", action=action, outcome="yes",
                                          quantity=10, limit_price=0.5, client_id="c", sent_at=at, status=status, answered_at=at,
                                          dollars=dollars))
    if settled:
        database.insert_settlement(conn, Settlement(t.id, settled[0], mode="live", yes_result="yes", yes_payout=settled[1]))
    return t


def test_results_count_even_trades_at_their_last_order_and_exposed_ones_once_settled(tmp_path):
    conn, b = setup(tmp_path)
    hour_ago = "2026-09-27T19:00:00+00:00"
    live_trade(conn, 10, 10, [("buy", 4.5, hour_ago, "filled"), ("buy", 4.7, hour_ago, "filled")])      # 10 matched pay 10: +0.80.
    live_trade(conn, 0, 0, [("buy", 4.5, hour_ago, "filled"), ("sell", 4.4, hour_ago, "filled")])       # Sold back a cent under: -0.10.
    live_trade(conn, 10, 0, [("buy", 4.5, hour_ago, "filled")])                                         # Exposed, so undecided.
    live_trade(conn, 10, 0, [("buy", 4.5, hour_ago, "filled")], settled=(hour_ago, 10.0))              # Exposed and won: +5.50.
    live_trade(conn, 10, 0, [("buy", 4.5, hour_ago, "filled"), ("buy", 0, hour_ago, "error")])         # Unknown, left to a human.
    live_trade(conn, 0, 0, [("buy", 0, hour_ago, "unfilled"), ("buy", 0, hour_ago, "unfilled")])       # Nothing filled: 0.
    live_trade(conn, 0, 0, [("buy", 4.5, "2026-09-27T13:00:00+00:00", "filled"), ("sell", 4.0, "2026-09-27T13:00:00+00:00", "filled")])
    results = b.results("2026-09-27T14:00:00+00:00")                   # The last trade was decided before the window.
    assert results == pytest.approx([0.8, -0.1, 5.5, 0.0])


def test_losing_more_than_a_tenth_of_the_live_money_in_the_window_halts(tmp_path):
    conn, b = setup(tmp_path)                                           # 100 dollars of cash, nothing open.
    live_trade(conn, 0, 0, [("buy", 20.0, "2026-09-27T19:00:00+00:00", "filled"), ("sell", 10.0, "2026-09-27T19:00:00+00:00", "filled")])
    b.check_results()
    assert b.halted is None                                             # 10 lost of the 110 there was, 9%.
    live_trade(conn, 0, 0, [("buy", 5.0, "2026-09-27T19:30:00+00:00", "filled"), ("sell", 3.0, "2026-09-27T19:30:00+00:00", "filled")])
    b.check_results()
    assert b.halted == "the live trades decided in the last 6 hours lost 12.00$, 11% of the live money, over the 10% limit"
    assert b.stopped is None                                            # Flattening goes on.


def test_the_brakes_start_over_after_a_halt(tmp_path):
    conn, b = setup(tmp_path)
    at = "2026-09-27T19:00:00+00:00"
    live_trade(conn, 0, 0, [("buy", 20.0, at, "filled"), ("sell", 5.0, at, "filled")])                  # 15 lost before the halt.
    database.insert_alert(conn, Alert(ts="2026-09-27T19:15:00+00:00", kind="halt", subject="halted", body="why"))
    for status in ("rejected", "rejected"):
        database.insert_order(conn, Order(trade_id=1, venue="kalshi", contract_id="k", purpose="open", action="buy", outcome="yes",
                                          quantity=1, limit_price=0.5, client_id="c", sent_at="2026-09-27T19:10:00+00:00", status=status))
    again = brakes.Brakes(conn, b.cash, lambda m: None, clock=lambda: NOW)      # Restarted once a human cleared the halt.
    again.check_results()
    assert again.halted is None                                         # The loss came before the halt.
    refused = Order(trade_id=1, venue="kalshi", contract_id="k", purpose="open", action="buy", outcome="yes", quantity=1,
                    limit_price=0.5, client_id="c", sent_at="2026-09-27T19:40:00+00:00", status="rejected")
    database.insert_order(conn, refused)
    again.watch(refused)
    assert again.halted is None                                         # One refusal since the halt, not three in a row.


def test_a_halt_on_results_keeps_flattening_until_one_on_orders_stops_it_across_restarts(tmp_path, halt_file):
    conn, b = setup(tmp_path)
    b.halt("lost too much")
    assert (b.halted, b.stopped) == ("lost too much", None)
    b.halt("lost even more")                                            # Only the first reason on results counts.
    restarted = brakes.Brakes(conn, b.cash, lambda m: None, clock=lambda: NOW)
    assert restarted.halted and restarted.stopped is None               # Still flattening after a restart.
    b.halt("kalshi refused 3 orders in a row", flatten=False)
    assert (b.halted, b.stopped) == ("lost too much", "kalshi refused 3 orders in a row")
    b.halt("polymarket_us refused 3 orders in a row", flatten=False)    # Nothing is left to stop.
    assert halt_file.read_text().splitlines() == ["2026-09-27T20:00:00 UTC new trades halted: lost too much",
                                                  "2026-09-27T20:00:00 UTC every order stopped: kalshi refused 3 orders in a row"]
    again = brakes.Brakes(conn, b.cash, lambda m: None, clock=lambda: NOW)      # Restarted before a human cleared it.
    assert again.halted == again.stopped == (f"halted before this start, remove {halt_file} to resume: 2026-09-27T20:00:00 UTC "
                                             "new trades halted: lost too much; "
                                             "2026-09-27T20:00:00 UTC every order stopped: kalshi refused 3 orders in a row")


def test_the_live_money_is_the_cash_and_what_open_trades_hold_at_cost(tmp_path):
    conn, b = setup(tmp_path)                                           # 100 dollars of cash.
    live_trade(conn, 10, 0, [("buy", 4.5, "2026-09-27T19:00:00+00:00", "filled")], yes_cost=4.5)
    assert b.capital() == pytest.approx(104.5)
