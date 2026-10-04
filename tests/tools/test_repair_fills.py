"""
Tests for repairing the live trades whose Polymarket US fills were misread, on a small database.
"""

import json
import pytest
from db import database
from db.models import Order, Trade
from tools import repair_fills

AT = "2026-09-30T17:06:55+00:00"
PAYS = "2026-11-28T23:59:00+00:00"


def piece(kind, shares, long_price, fee):
    return {"type": f"EXECUTION_TYPE_{kind}", "lastShares": shares, "lastPx": {"value": long_price}, "commissionNotionalCollected": {"value": fee}}


def trade(conn, pair_id, yes, no, quantity, **stored):
    """
    Store a live trade between two Polymarket US markets, yes through the first and no through the other side of the second.
    """
    t = Trade(mode="live", pair_id=pair_id, trade="t", signal_ts=AT, edge=0.06, quantity=quantity, pays_at=PAYS,
              yes_venue="polymarket_us", yes_contract=yes, yes_polarity="yes", yes_limit=0.8,
              no_venue="polymarket_us", no_contract=no, no_polarity="yes", no_limit=0.12, **stored)
    database.insert_trade(conn, t)
    return t.id


def order(conn, trade_id, contract, purpose, action, outcome, quantity, executions, status, filled, dollars):
    """
    Store an order with the answer it got and what the old reading made of it.
    """
    database.insert_order(conn, Order(trade_id=trade_id, venue="polymarket_us", contract_id=contract, purpose=purpose, action=action,
                                      outcome=outcome, quantity=quantity, limit_price=0.5, client_id="c", sent_at=AT, status=status,
                                      filled=filled, dollars=dollars, response=json.dumps({"id": "x", "executions": executions})))


@pytest.fixture
def conn(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    # As trade 9 went: yes filled 3 as 0.1 and 2.9, read as 2, so the 1 no over was sold back, leaving the trade 1 yes over.
    t = trade(conn, 1, "a", "b", 3, yes_filled=2, yes_cost=1.63, no_filled=3, no_cost=0.26, yes_held=2, no_held=2, matched=2,
              profit=2 * (1 - 0.815 - 0.13), hedge_pnl=0.10 - 0.13, status="partial", hedge="sold back 1 of 1 on polymarket_us")
    order(conn, t, "a", "open", "buy", "yes", 3, [piece("PARTIAL_FILL", "0.1", "0.80", "0"), piece("FILL", "2.9", "0.80", "0.03")],
          "partial", 2, 1.63)
    order(conn, t, "b", "open", "buy", "no", 3, [piece("FILL", "3", "0.88", "0.03")], "filled", 3, 0.39)
    order(conn, t, "b", "flatten", "sell", "no", 1, [piece("FILL", "1", "0.89", "0.01")], "filled", 1, 0.10)
    # As trade 26 went: no filled 1 in pieces, read as none, so yes was sold back twice, each sale read as none though it sold.
    t = trade(conn, 2, "c", "d", 1, yes_filled=1, yes_cost=0.73, no_filled=0, no_cost=0.01, status="failed",
              hedge="sold back 0 of 1 on polymarket_us, 1 exposed, then sold back 0 of 1")
    in_pieces = [piece("PARTIAL_FILL", "0.1", "0.71", "0"), piece("FILL", "0.9", "0.71", "0.01")]
    order(conn, t, "c", "open", "buy", "yes", 1, [piece("FILL", "1", "0.72", "0.01")], "filled", 1, 0.73)
    order(conn, t, "d", "open", "buy", "no", 1, [piece("PARTIAL_FILL", "0.1", "0.86", "0"), piece("FILL", "0.9", "0.86", "0.01")],
          "unfilled", 0, 0.01)
    order(conn, t, "c", "flatten", "sell", "yes", 1, in_pieces, "unfilled", 0, -0.01)
    order(conn, t, "c", "flatten", "sell", "yes", 1, in_pieces, "unfilled", 0, -0.01)
    # Another trade on the same bet, and one on a third that was read right.
    t = trade(conn, 2, "c", "d", 1, yes_filled=1, yes_cost=0.72, no_filled=1, no_cost=0.14, yes_held=1, no_held=1, matched=1,
              profit=0.14, status="filled")
    order(conn, t, "c", "open", "buy", "yes", 1, [piece("FILL", "1", "0.72", "0")], "filled", 1, 0.72)
    order(conn, t, "d", "open", "buy", "no", 1, [piece("FILL", "1", "0.86", "0")], "filled", 1, 0.14)
    t = trade(conn, 3, "e", "f", 2, yes_filled=2, yes_cost=1.0, no_filled=2, no_cost=0.9, yes_held=2, no_held=2, matched=2,
              profit=2 * (1 - 0.5 - 0.45), status="filled")
    order(conn, t, "e", "open", "buy", "yes", 2, [piece("FILL", "2", "0.50", "0")], "filled", 2, 1.0)
    order(conn, t, "f", "open", "buy", "no", 2, [piece("FILL", "2", "0.55", "0")], "filled", 2, 0.9)
    return conn


VENUE = {"polymarket_us": lambda: {"a": 3.0, "b": -2.0, "c": -1.0, "d": -2.0, "e": 2.0, "f": -2.0}, "kalshi": lambda: {}}


def stored(conn, trade_id):
    return dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())


def test_without_apply_it_writes_nothing(conn):
    before = [dict(r) for r in conn.execute("SELECT * FROM trades")] + [dict(r) for r in conn.execute("SELECT * FROM orders")]
    lines = []
    repair_fills.repair(conn, False, VENUE, lines.append)
    assert [dict(r) for r in conn.execute("SELECT * FROM trades")] + [dict(r) for r in conn.execute("SELECT * FROM orders")] == before
    assert "nothing written: run again with --apply to write the repaired orders and trades" in lines


def test_a_misread_order_and_its_trade_are_worked_out_again(conn):
    lines = []
    assert repair_fills.repair(conn, True, VENUE, lines.append) == [2, 3]
    t = stored(conn, 1)
    # Yes filled all 3 at 0.80 and 0.03 of fees, so the trade matched 3 and, with the no sold back, holds 1 yes over.
    assert (t["yes_filled"], t["no_filled"], t["yes_held"], t["no_held"], t["matched"], t["status"]) == (3, 3, 3, 2, 3, "filled")
    assert t["yes_cost"] == pytest.approx(2.43) and t["no_cost"] == pytest.approx(0.26)
    assert t["profit"] == pytest.approx(3 * (1 - 0.81 - 0.13)) and t["hedge_pnl"] == pytest.approx(-0.03)
    assert t["hedge"] == "sold back 1 of 1 on polymarket_us; repaired from the venue's answers"
    first = dict(conn.execute("SELECT * FROM orders WHERE id = 1").fetchone())
    assert (first["status"], first["filled"], first["dollars"]) == ("filled", 3, pytest.approx(2.43))
    assert "order 1 of trade 1: buy 3 yes partial 2 -> filled 3, 1.6300$ -> 2.4300$" in lines
    # Its trades now hold what the venue does, and the trades left for a human do not.
    assert [line for line in lines if line.startswith("position")] == ["position polymarket_us c: the trades hold 1, the venue -1",
                                                                       "position polymarket_us d: the trades hold -1, the venue -2"]


def test_a_bet_with_a_leg_sold_below_nothing_is_left_for_a_human(conn):
    lines = []
    repair_fills.repair(conn, True, VENUE, lines.append)
    assert "trade 2 t: left for a human, order 7 sold 1 of the yes leg, which held 0" in lines
    assert "trade 3 t: left for a human, on the same bet as one that was oversold" in lines
    assert (stored(conn, 2)["no_filled"], stored(conn, 2)["status"]) == (0, "failed")          # As it was.
    assert dict(conn.execute("SELECT status, filled FROM orders WHERE id = 5").fetchone()) == {"status": "unfilled", "filled": 0}
    assert stored(conn, 4)["status"] == "filled"                                             # Read right, left alone.


def test_a_trade_named_is_worked_out_again_though_no_order_was_misread(conn):
    # The record of the trade read right says it still holds 2 a side, though its orders sold 1 yes back since.
    database.insert_order(conn, Order(trade_id=4, venue="polymarket_us", contract_id="e", purpose="flatten", action="sell", outcome="yes",
                                      quantity=1, limit_price=0.5, client_id="c", sent_at=AT, status="filled", filled=1, dollars=0.49, fees=0.01,
                                      response=json.dumps({"id": "x", "executions": [piece("FILL", "1", "0.50", "0.01")]})))
    with pytest.raises(ValueError, match="trade 4 has no misread order, yet works out differently again"):
        repair_fills.repair(conn, True, VENUE, lambda line: None)
    lines = []
    repair_fills.repair(conn, True, VENUE, lines.append, rewrite=[4])
    t = stored(conn, 4)
    assert (t["yes_held"], t["no_held"], t["hedge_pnl"]) == (1, 2, pytest.approx(0.49 - 0.5))
    assert t["hedge"].endswith("; worked out again from its orders")
