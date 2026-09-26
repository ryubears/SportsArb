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
    conn.execute("INSERT INTO pairs (label, kind, venues, contracts, flags, matched_at) VALUES (?, 'game_winner', 'kalshi,polymarket_us', 2, '[]', ?)",
                 ("game_winner 2026-09-20 CAR@ATL CAR", KICKOFF))
    t = Trade(pair_id=1, label="game_winner 2026-09-20 CAR@ATL CAR", trade="yes: PMUS buy, no: K buy other side",
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
    row = conn.execute("SELECT * FROM trades").fetchone()
    # The bet resolved yes. The Polymarket US leg held the yes side and is paid a dollar each. The Kalshi leg held no and gets nothing.
    assert (row["yes_result"], row["yes_payout"], row["yes_settled_at"]) == ("yes", 50, "2026-09-20T20:10:00+00:00")
    assert (row["no_result"], row["no_payout"], row["no_settled_at"]) == ("yes", 0, "2026-09-20T20:09:00+00:00")
    assert row["settled_at"] == "2026-09-20T20:10:00+00:00"       # The later leg.
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 + 50, "kalshi": 10000})
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason, trade_id FROM ledger")] == [("polymarket_us", 50.0, "payout", trade.id)]
    assert logs == [f"settled {trade.label}: polymarket_us yes yes pays 50$, kalshi no yes pays 0$, realized +4.00$"]
    assert s.summary() == "settled: 1 trades for +4.00$, 0 still open"


def test_a_trade_is_checked_from_kickoff_when_its_contracts_have_a_start_time(tmp_path):
    from db.models import Contract
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    s = settle.Settler(conn, cash, lambda m: None)
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time=PAYS_AT, fee_info=None) for v, c in (("polymarket_us", "pm"), ("kalshi", "k"))], KICKOFF)
    filled_trade(conn)
    assert database.load_open_trades(conn)[0].starts_at == KICKOFF
    results = {("polymarket_us", "pm"): ("yes", "2026-09-20T17:20:00+00:00"), ("kalshi", "k"): ("yes", "2026-09-20T17:19:00+00:00")}
    settled(s, "2026-09-20T16:59:00+00:00", results)                  # Before kickoff, not looked at.
    assert conn.execute("SELECT settled_at FROM trades").fetchone()[0] is None
    settled(s, "2026-09-20T17:30:00+00:00", results)                  # During the game, a decided prop settles at once.
    assert conn.execute("SELECT settled_at FROM trades").fetchone()[0] == "2026-09-20T17:20:00+00:00"
    assert cash["polymarket_us"] == pytest.approx(10050)


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
