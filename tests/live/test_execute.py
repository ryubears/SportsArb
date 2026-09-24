"""
Tests for the paper executor, with fixed latency and no randomness unless a test asks for it.
"""

import asyncio
import random
import pytest
from db import database
from db.models import Quote
from live import balances, execute, scan

NO_PM_FEES = {"feeCoefficient": 0}
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}
KICKOFF = "2026-09-20T17:00:00+00:00"
NOW = "2026-09-20T17:30:00+00:00"
PAIR = {"label": "game_winner 2026-09-20 CAR@ATL CAR", "kind": "game_winner"}
YES = {"venue": "polymarket_us", "contract_id": "pm", "polarity": "yes", "start_time": KICKOFF, "close_time": "2026-09-20T21:00:00+00:00"}
NO = {"venue": "kalshi", "contract_id": "k", "polarity": "yes", "start_time": KICKOFF, "close_time": "2026-09-20T21:00:00+00:00"}
FEES = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES}


def books(pm_bid=0.44, pm_ask=0.45, k_bid=0.53, k_ask=0.54, size=100):
    """
    Books where yes is cheapest on Polymarket US at the ask and no is cheapest on Kalshi through the bid.
    """
    return {("polymarket_us", "pm"): Quote("polymarket_us", "pm", NOW, [[pm_bid, size]], [[pm_ask, size]]),
            ("kalshi", "k"): Quote("kalshi", "k", NOW, [[k_bid, size]], [[k_ask, size]])}


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(execute, "LATENCY_MS", {"kalshi": (1, 0), "polymarket_us": (1, 0)})
    monkeypatch.setattr(execute, "REJECT_PROBABILITY", 0)


def executor(tmp_path, latest, log=lambda m: None, start=balances.BALANCE):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn, start)
    return conn, cash, execute.PaperExecutor(conn, cash, lambda: latest, log, random.Random(1))


def run(ex, after_signal=None, signals=1):
    """
    Send the signal inside a loop, optionally change the books before the orders arrive, and wait for the trades.
    """
    async def scenario():
        sent = [ex.signal(PAIR, YES, NO, 1 - 0.45 - 0.47, 100, FEES, NOW) for _ in range(signals)]
        if after_signal:
            after_signal()
        if ex.tasks:
            await asyncio.gather(*ex.tasks)
        return sent
    return asyncio.run(scenario())


def stored(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")]


def test_unchanged_books_fill_both_legs_and_lock_in_the_edge(tmp_path, quick):
    latest = books()
    logs = []
    conn, cash, ex = executor(tmp_path, latest, logs.append)
    assert run(ex) == [True]
    t = stored(conn)[0]
    assert (t["status"], t["quantity"], t["yes_filled"], t["no_filled"], t["matched"]) == ("filled", 50, 50, 50, 50)   # Half the visible 100.
    assert (t["yes_limit"], t["no_limit"], t["yes_polarity"], t["no_polarity"]) == (0.45, 0.47, "yes", "yes")
    assert t["profit"] == pytest.approx(50 * (1 - 0.45 - 0.47))
    assert (t["hedge"], t["hedge_pnl"], t["yes_held"], t["no_held"]) == ("none", 0, 50, 50)
    assert t["pays_at"] == "2026-09-20T21:00:00+00:00"
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 - 50 * 0.45, "kalshi": 10000 - 50 * 0.47})
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason, trade_id FROM ledger ORDER BY id")] == [
        ("polymarket_us", pytest.approx(-22.5), "buy", 1), ("kalshi", pytest.approx(-23.5), "buy", 1)]
    assert logs[0].startswith("paper filled: game_winner 2026-09-20 CAR@ATL CAR")
    assert ex.summary().startswith("paper: 1 trades (1 filled, 0 partial, 0 failed), locked in 4.00$, hedges +0.00$; total 1 trades, 4.00$")


def test_a_shrunken_leg_is_completed_on_the_other_venue_when_that_is_cheaper(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)

    def kalshi_thins_out():
        latest[("kalshi", "k")] = Quote("kalshi", "k", NOW, [[0.53, 40]], [[0.54, 100]])

    run(ex, kalshi_thins_out)
    t = stored(conn)[0]
    assert (t["yes_filled"], t["no_filled"]) == (50, 20)
    # Selling 30 yes back at the 0.44 bid loses a cent each. Buying more no on Kalshi at 0.47 still earns 8 cents each, so it wins,
    # but only 20 more are there for us, leaving 10 exposed.
    assert t["hedge"] == "bought 20 of 30 on kalshi, 10 exposed"
    assert t["hedge_pnl"] == pytest.approx(20 * (1 - 0.45 - 0.47))
    assert (t["matched"], t["status"], t["yes_held"], t["no_held"]) == (40, "partial", 50, 40)
    assert cash["kalshi"] == pytest.approx(10000 - 40 * 0.47)


def test_a_leg_with_no_book_is_flattened_by_selling_the_other_back(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    run(ex, lambda: latest.pop(("kalshi", "k")))
    t = stored(conn)[0]
    assert (t["yes_filled"], t["no_filled"], t["matched"], t["status"]) == (50, 0, 0, "failed")
    assert t["hedge"] == "sold back 50 of 50 on polymarket_us, no leg no book"
    assert t["hedge_pnl"] == pytest.approx(50 * (0.44 - 0.45))
    assert (t["yes_held"], t["no_held"]) == (0, 0)
    assert cash["polymarket_us"] == pytest.approx(10000 - 50 * 0.45 + 50 * 0.44)
    assert cash["kalshi"] == pytest.approx(10000)


def test_rejected_orders_fail_without_a_hedge(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(execute, "REJECT_PROBABILITY", 1)
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    run(ex)
    t = stored(conn)[0]
    assert (t["status"], t["matched"], t["hedge"]) == ("failed", 0, "yes leg rejected, no leg rejected")
    assert cash.amounts == pytest.approx({"polymarket_us": 10000, "kalshi": 10000})


def test_signal_is_refused_for_thin_edges_and_slow_payouts(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    assert ex.signal(PAIR, YES, NO, 0.015, 100, FEES, NOW) is False
    future = dict(YES, start_time=None, close_time="2027-02-14T00:00:00+00:00")
    assert ex.signal(PAIR, future, dict(NO, start_time=None, close_time="2027-02-14T00:00:00+00:00"), 0.05, 100, FEES, NOW) is False
    assert ex.tasks == set() and stored(conn) == []


def test_two_signals_at_once_share_the_balance_instead_of_both_spending_it(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest, start=30.0)      # Room for 50 contracts at 0.45 once, not twice.
    assert run(ex, signals=2) == [True, True]
    first, second = stored(conn)
    # The second saw what the first had reserved on both venues: 7.5 left on Polymarket US and 6.5 on Kalshi, so 13 at 0.47.
    assert (first["quantity"], second["quantity"]) == (50, 13)
    assert cash.amounts == pytest.approx({"polymarket_us": 30 - 63 * 0.45, "kalshi": 30 - 63 * 0.47})
    assert min(cash.amounts.values()) >= 0


def test_a_trade_is_stored_as_sent_before_it_fills(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)

    async def scenario():
        ex.signal(PAIR, YES, NO, 0.08, 100, FEES, NOW)
        return stored(conn)[0]["status"]

    assert asyncio.run(scenario()) == "sent"


def test_latency_is_drawn_around_the_measured_median(tmp_path):
    conn, cash, ex = executor(tmp_path, {})
    ex.rng = random.Random(7)
    draws = [ex.latency("kalshi") for _ in range(200)]
    assert 40 < sorted(draws)[100] < 90 and min(draws) > 10 and max(draws) < 400


def test_scanner_signals_once_per_episode(tmp_path):
    from db.models import Bet, Contract, Pair
    conn = database.connect(tmp_path / "t.sqlite")
    members = [("kalshi", "k", NO_K_FEES), ("polymarket_us", "pm", NO_PM_FEES)]
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time="2026-09-20T21:00:00+00:00", fee_info=f) for v, c, f in members], NOW)
    bets = [Bet(v, c, "game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, "yes") for v, c, _ in members]
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", [Pair(PAIR["label"], "game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, bets, [])], NOW)
    calls = []
    s = scan.Scanner(conn, "nfl", lambda m: None,
                     on_signal=lambda pair, yes, no, edge, size, fee_infos, now: calls.append((pair["label"], round(edge, 2), now)) or True)
    latest = books()
    s.on_book("polymarket_us", "pm", latest, NOW)
    latest[("polymarket_us", "pm")] = Quote("polymarket_us", "pm", NOW, [[0.40, 100]], [[0.41, 100]])
    s.on_book("polymarket_us", "pm", latest, "2026-09-20T17:30:01+00:00")     # A bigger edge in the same episode brings no second signal.
    assert calls == [(PAIR["label"], 0.08, NOW)]
