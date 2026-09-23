"""
Tests for settling paper trades with canned venue results.
"""

import asyncio
import pytest
from db import database
from db.models import Trade
from live import balances, settle

KICKOFF = "2026-09-20T17:00:00+00:00"
PAYS_AT = "2026-09-20T21:00:00+00:00"


def filled_trade(conn):
    """
    A done trade: 50 yes on Polymarket US at 0.45 and 50 no on Kalshi at 0.47, held through a yes contract on each venue.
    """
    t = Trade(label="game_winner 2026-09-20 CAR@ATL CAR", kind="game_winner", trade="yes: PMUS buy, no: K buy other side",
              signal_ts="2026-09-20T17:30:00+00:00", edge=0.08, quantity=50,
              yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45,
              no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.47, pays_at=PAYS_AT,
              yes_filled=50, yes_cost=22.5, no_filled=50, no_cost=23.5, yes_held=50, no_held=50, matched=50, profit=4.0, status="filled")
    database.insert_trade(conn, t)
    return t


def settled(settler, now, results):
    """
    Run a settlement check with canned results, given as {(venue, contract_id): (result, settled_at)}.
    """
    settler.results = {"kalshi": lambda ids: {c: r for (v, c), r in results.items() if v == "kalshi" and c in ids},
                       "polymarket_us": lambda events: {c: r for (v, c), r in results.items() if v == "polymarket_us"}}
    asyncio.run(settler.settle(now))


def test_settlement_pays_the_winning_leg_only_and_records_each_leg(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    logs = []
    s = settle.Settler(conn, cash, logs.append)
    trade = filled_trade(conn)
    settled(s, "2026-09-20T20:00:00+00:00", {})                 # Not due yet.
    assert conn.execute("SELECT settled_at FROM trades").fetchone()[0] is None
    settled(s, "2026-09-20T21:05:00+00:00",
            {("polymarket_us", "pm"): ("yes", "2026-09-20T20:10:00+00:00"), ("kalshi", "k"): ("yes", "2026-09-20T20:09:00+00:00")})
    assert conn.execute("SELECT settled_at FROM trades").fetchone()[0] == "2026-09-20T20:10:00+00:00"
    legs = {r["venue"]: dict(r) for r in conn.execute("SELECT * FROM settlements")}
    # The bet resolved yes. The Polymarket US leg held the yes side and is paid a dollar each. The Kalshi leg held no and gets nothing.
    assert (legs["polymarket_us"]["payout"], legs["polymarket_us"]["realized"]) == (50, pytest.approx(50 - 22.5))
    assert (legs["kalshi"]["payout"], legs["kalshi"]["realized"]) == (0, pytest.approx(-23.5))
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 + 50, "kalshi": 10000})
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason, trade_id FROM ledger")] == [("polymarket_us", 50.0, "payout", trade.id)]
    assert logs == [f"settled {trade.label}: polymarket_us yes yes pays 50$, kalshi no yes pays 0$, realized +4.00$"]
    assert s.summary() == "settled: 1 trades for +4.00$, 0 still open"


def test_a_trade_waits_until_every_held_leg_has_a_result(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    s = settle.Settler(conn, balances.Balances(conn), lambda m: None)
    filled_trade(conn)
    settled(s, "2026-09-20T21:05:00+00:00", {("kalshi", "k"): ("no", "2026-09-20T20:09:00+00:00")})
    assert conn.execute("SELECT settled_at FROM trades").fetchone()[0] is None
    assert s.summary() == "settled: 0 trades for +0.00$, 1 still open"


def test_leg_won():
    assert settle.leg_won("yes", "yes", "yes") and settle.leg_won("no", "yes", "no")
    assert not settle.leg_won("yes", "yes", "no") and not settle.leg_won("no", "yes", "yes")
    assert settle.leg_won("no", "no", "yes") and not settle.leg_won("yes", "no", "yes")
