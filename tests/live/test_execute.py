"""
Tests for the paper executor, with fixed latency and no randomness unless a test asks for it.
"""

import asyncio
import random
import pytest
from db import database
from db.models import Quote
from live import execute, scan

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


def run(executor, latest, after_signal=None):
    """
    Send the signal inside a loop, optionally change the books before the orders arrive, and wait for the trade.
    """
    async def scenario():
        sent = executor.signal(PAIR, YES, NO, 1 - 0.45 - 0.47, 100, FEES, NOW)
        if after_signal:
            after_signal()
        if executor.tasks:
            await asyncio.gather(*executor.tasks)
        return sent
    return asyncio.run(scenario())


def stored(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM trades")]


def test_unchanged_books_fill_both_legs_and_lock_in_the_edge(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    latest = books()
    logs = []
    ex = execute.PaperExecutor(conn, lambda: latest, logs.append, random.Random(1))
    assert run(ex, latest) is True
    t = stored(conn)[0]
    assert (t["status"], t["quantity"], t["yes_filled"], t["no_filled"], t["matched"]) == ("filled", 50, 50, 50, 50)   # Half the visible 100.
    assert (t["yes_limit"], t["no_limit"]) == (0.45, 0.47)
    assert t["profit"] == pytest.approx(50 * (1 - 0.45 - 0.47))
    assert (t["hedge"], t["hedge_pnl"]) == ("none", 0)
    assert t["pays_at"] == "2026-09-20T21:00:00+00:00"
    assert ex.balances == pytest.approx({"polymarket_us": 5000 - 50 * 0.45, "kalshi": 5000 - 50 * 0.47})
    assert logs[0].startswith("paper filled: game_winner 2026-09-20 CAR@ATL CAR")


def test_a_shrunken_leg_is_completed_on_the_other_venue_when_that_is_cheaper(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    latest = books()
    ex = execute.PaperExecutor(conn, lambda: latest, lambda m: None, random.Random(1))

    def kalshi_thins_out():
        latest[("kalshi", "k")] = Quote("kalshi", "k", NOW, [[0.53, 40]], [[0.54, 100]])

    run(ex, latest, kalshi_thins_out)
    t = stored(conn)[0]
    assert (t["yes_filled"], t["no_filled"]) == (50, 20)
    # Selling 30 yes back at the 0.44 bid loses a cent each. Buying more no on Kalshi at 0.47 still earns 8 cents each, so it wins,
    # but only 20 more are there for us, leaving 10 exposed.
    assert t["hedge"] == "bought 20 of 30 on kalshi, 10 exposed"
    assert t["hedge_pnl"] == pytest.approx(20 * (1 - 0.45 - 0.47))
    assert (t["matched"], t["status"]) == (40, "partial")


def test_a_leg_with_no_book_is_flattened_by_selling_the_other_back(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    latest = books()
    ex = execute.PaperExecutor(conn, lambda: latest, lambda m: None, random.Random(1))
    run(ex, latest, lambda: latest.pop(("kalshi", "k")))
    t = stored(conn)[0]
    assert (t["yes_filled"], t["no_filled"], t["matched"], t["status"]) == (50, 0, 0, "failed")
    assert t["hedge"] == "sold back 50 of 50 on polymarket_us, no leg no book"
    assert t["hedge_pnl"] == pytest.approx(50 * (0.44 - 0.45))
    assert ex.balances["polymarket_us"] == pytest.approx(5000 - 50 * 0.45 + 50 * 0.44)
    assert ex.balances["kalshi"] == pytest.approx(5000)


def test_rejected_orders_fail_without_a_hedge(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(execute, "REJECT_PROBABILITY", 1)
    conn = database.connect(tmp_path / "t.sqlite")
    latest = books()
    ex = execute.PaperExecutor(conn, lambda: latest, lambda m: None, random.Random(1))
    run(ex, latest)
    t = stored(conn)[0]
    assert (t["status"], t["matched"], t["hedge"]) == ("failed", 0, "yes leg rejected, no leg rejected")
    assert ex.balances == pytest.approx({"polymarket_us": 5000, "kalshi": 5000})


def test_signal_is_refused_for_thin_edges_and_slow_payouts(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    latest = books()
    ex = execute.PaperExecutor(conn, lambda: latest, lambda m: None)
    assert ex.signal(PAIR, YES, NO, 0.01, 100, FEES, NOW) is False
    future = dict(YES, start_time=None, close_time="2027-02-14T00:00:00+00:00")
    assert ex.signal(PAIR, future, dict(NO, start_time=None, close_time="2027-02-14T00:00:00+00:00"), 0.05, 100, FEES, NOW) is False
    assert ex.tasks == set() and stored(conn) == []


def test_latency_is_drawn_around_the_measured_median(tmp_path):
    ex = execute.PaperExecutor(database.connect(tmp_path / "t.sqlite"), dict, lambda m: None, random.Random(7))
    draws = [ex.latency("kalshi") for _ in range(200)]
    assert 40 < sorted(draws)[100] < 90 and min(draws) > 10 and max(draws) < 400


def settle(ex, now, results):
    """
    Run a settlement check with canned venue results, given as {(venue, contract_id): (result, settled_at)}.
    """
    ex.results = {"kalshi": lambda ids: {c: r for (v, c), r in results.items() if v == "kalshi" and c in ids},
                  "polymarket_us": lambda events: {c: r for (v, c), r in results.items() if v == "polymarket_us"}}
    asyncio.run(ex.settle(now))


def test_settlement_pays_the_winning_leg_only_and_records_each_leg(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    latest = books()
    logs = []
    ex = execute.PaperExecutor(conn, lambda: latest, logs.append, random.Random(1))
    run(ex, latest)                                             # 50 yes on Polymarket US at 0.45, 50 no on Kalshi through the 0.53 bid.
    settle(ex, "2026-09-20T20:00:00+00:00", {})                 # Not due yet.
    assert stored(conn)[0]["settled_at"] is None
    when = "2026-09-20T21:05:00+00:00"
    settle(ex, when, {("polymarket_us", "pm"): ("yes", "2026-09-20T20:10:00+00:00"), ("kalshi", "k"): ("yes", "2026-09-20T20:09:00+00:00")})
    t = stored(conn)[0]
    assert t["settled_at"] == "2026-09-20T20:10:00+00:00"
    legs = {r["venue"]: dict(r) for r in conn.execute("SELECT * FROM settlements")}
    # The bet resolved yes. The Polymarket US leg held the yes side and is paid a dollar each. The Kalshi leg held no and gets nothing.
    assert (legs["polymarket_us"]["payout"], legs["polymarket_us"]["realized"]) == (50, pytest.approx(50 - 50 * 0.45))
    assert (legs["kalshi"]["payout"], legs["kalshi"]["realized"]) == (0, pytest.approx(-50 * 0.47))
    assert ex.balances == pytest.approx({"polymarket_us": 5000 - 22.5 + 50, "kalshi": 5000 - 23.5})
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason FROM ledger ORDER BY id")] == [
        ("polymarket_us", pytest.approx(-22.5), "buy"), ("kalshi", pytest.approx(-23.5), "buy"), ("polymarket_us", 50.0, "payout")]
    assert any(l.startswith("settled game_winner 2026-09-20 CAR@ATL CAR: polymarket_us yes yes pays 50$, kalshi no yes pays 0$, realized +4.00$") for l in logs)
    assert "settled 1 for +4.00$" in ex.summary()


def test_balances_survive_a_restart_through_the_ledger(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    latest = books()
    ex = execute.PaperExecutor(conn, lambda: latest, lambda m: None, random.Random(1))
    run(ex, latest)
    again = execute.PaperExecutor(conn, lambda: latest, lambda m: None)
    assert again.balances == pytest.approx(ex.balances)


def test_weekly_check_moves_the_excess_and_it_lands_after_four_business_days(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    logs = []
    ex = execute.PaperExecutor(conn, dict, logs.append)
    ex.balances = {"kalshi": 3000.0, "polymarket_us": 7000.0}     # 2000 above a 5000 average, past the 25 percent drift.
    ex.rebalance("2026-09-20T12:00:00+00:00")                       # A Sunday, no check.
    assert database.load_transfers(conn) == []
    ex.rebalance("2026-09-21T12:00:00+00:00")                       # Monday.
    (transfer,) = database.load_transfers(conn)
    assert (transfer.from_venue, transfer.to_venue, transfer.amount, transfer.reason) == ("polymarket_us", "kalshi", 2000, "drift")
    assert transfer.expected_at == "2026-09-25T12:00:00+00:00"
    assert ex.balances == {"kalshi": 3000.0, "polymarket_us": 5000.0}     # In transit, on neither venue.
    ex.rebalance("2026-09-21T13:00:00+00:00")                       # Same Monday, one transfer at a time anyway.
    assert len(database.load_transfers(conn)) == 1
    ex.receive_transfers("2026-09-25T11:00:00+00:00")
    assert ex.balances["kalshi"] == 3000.0
    ex.receive_transfers("2026-09-25T12:00:00+00:00")
    assert ex.balances == {"kalshi": 5000.0, "polymarket_us": 5000.0}
    assert database.load_transfers(conn)[0].arrived_at == "2026-09-25T12:00:00+00:00"
    assert "2,000$ in transit" not in ex.summary()


def test_a_venue_under_the_floor_is_topped_up_on_any_day(tmp_path, quick):
    conn = database.connect(tmp_path / "t.sqlite")
    ex = execute.PaperExecutor(conn, dict, lambda m: None)
    ex.balances = {"kalshi": 400.0, "polymarket_us": 6000.0}
    ex.rebalance("2026-09-23T12:00:00+00:00")                       # A Wednesday.
    (transfer,) = database.load_transfers(conn)
    assert (transfer.reason, transfer.amount) == ("floor", 2800)
    assert "2,800$ in transit" in ex.summary()


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
