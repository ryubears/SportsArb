"""
Tests for the paper executor, with fixed latency and no randomness unless a test asks for it.
"""

import asyncio
import random
import pytest
from db import database
from db.models import Book
from engine.components import scan, settle
from engine.components.balance.paper import PaperBalances
from engine.components.execute.paper import PaperExecutor
from engine.helper import config
from trade_setup import FEES, KICKOFF, NO, NO_K_FEES, NO_PM_FEES, NOW, PAIR, YES, books, stored


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(config, "LATENCY_MS", {"kalshi": (1, 0), "polymarket_us": (1, 0)})
    monkeypatch.setattr(config, "REJECT_PROBABILITY", 0)


def executor(tmp_path, latest, log=lambda m: None, start=config.START_BALANCE, clock=lambda: NOW):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn, start)
    return conn, cash, PaperExecutor(conn, cash, lambda: latest, log, random.Random(1), clock=clock)


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


def test_unchanged_books_fill_both_legs_and_lock_in_the_edge(tmp_path, quick):
    latest = books()
    logs = []
    conn, cash, ex = executor(tmp_path, latest, logs.append)
    assert run(ex) == [True]
    t = stored(conn)[0]
    assert (t["mode"], t["status"], t["quantity"], t["yes_filled"], t["no_filled"], t["matched"]) == ("paper", "filled", 50, 50, 50, 50)   # Half the visible 100.
    assert (t["yes_limit"], t["no_limit"], t["yes_polarity"], t["no_polarity"]) == (0.45, 0.47, "yes", "yes")
    assert t["profit"] == pytest.approx(50 * (1 - 0.45 - 0.47))
    assert (t["hedge"], t["hedge_pnl"], t["yes_held"], t["no_held"]) == ("none", 0, 50, 50)
    assert t["pays_at"] == "2026-09-20T20:45:00+00:00"            # Kickoff plus the game and the venues settling.
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 - 50 * 0.45, "kalshi": 10000 - 50 * 0.47})
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason, trade_id FROM ledger ORDER BY id")][2:] == [
        ("polymarket_us", pytest.approx(-22.5), "buy", 1), ("kalshi", pytest.approx(-23.5), "buy", 1)]      # After the two openings.
    assert logs[0].startswith("paper filled: nfl game_winner 2026-09-20 CAR@ATL CAR")
    assert ex.summary().startswith("paper: 1 trades (1 filled, 0 partial, 0 failed), locked in 4.00$, hedges +0.00$; total 1 trades, 4.00$")


def test_an_order_sweeps_the_levels_that_keep_the_edge_floor_and_skips_a_one_lot_top(tmp_path, quick):
    latest = books()
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.44, 100]], [[0.45, 1], [0.46, 100], [0.50, 100]])
    conn, cash, ex = executor(tmp_path, latest)
    assert run(ex) == [True]
    t = stored(conn)[0]
    # The 1-lot at 0.45 fills nothing for us. The 100 at 0.46 still leave 7 cents against 0.47, so the limit reaches it.
    # The 0.50 level would leave 3 cents and is left out. Half of the 100 contracts inside the limit is the quantity.
    assert (t["yes_limit"], t["no_limit"], t["quantity"]) == (0.46, 0.47, 50)
    assert (t["status"], t["yes_filled"], t["no_filled"], t["matched"]) == ("filled", 50, 50, 50)
    assert t["yes_cost"] == pytest.approx(50 * 0.46) and t["profit"] == pytest.approx(50 * (1 - 0.46 - 0.47))


def test_a_shrunken_leg_is_completed_on_the_other_venue_when_that_is_cheaper(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)

    def kalshi_thins_out():
        latest[("kalshi", "k")] = Book("kalshi", "k", NOW, [[0.53, 40]], [[0.54, 100]])

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


def test_exposure_is_flattened_on_a_later_tick_once_a_book_allows_it(tmp_path, quick):
    latest = books()
    logs = []
    conn, cash, ex = executor(tmp_path, latest, logs.append)

    def kalshi_vanishes_and_polymarket_loses_its_bids():
        latest.pop(("kalshi", "k"))
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])

    run(ex, kalshi_vanishes_and_polymarket_loses_its_bids)
    t = stored(conn)[0]
    assert (t["yes_held"], t["no_held"], t["hedge"]) == (50, 0, "50 exposed, no book to flatten, no leg no book")
    assert list(ex.exposed) == [t["id"]]

    async def later():
        ex.tick(NOW)                                            # Still no book, nothing to do.
        await asyncio.gather(*ex.tasks)
        latest.update(books(k_bid=0.53, k_ask=0.54, size=40))   # Kalshi is back with 40 on the ask, 20 for us.
        ex.tick("2026-09-20T17:31:00+00:00")
        await asyncio.gather(*ex.tasks)
        ex.tick("2026-09-20T21:30:00+00:00")                    # Past the payout, the rest is left to settle.
        await asyncio.gather(*ex.tasks)
    asyncio.run(later())
    t = stored(conn)[0]
    assert (t["yes_held"], t["no_held"], t["matched"], t["status"]) == (50, 20, 20, "partial")
    assert t["hedge"] == "50 exposed, no book to flatten, no leg no book, then bought 20 of 50 on kalshi, 30 exposed at 17:31:00"
    assert t["hedge_pnl"] == pytest.approx(20 * (1 - 0.45 - 0.47))
    assert logs[-1] == "paper flattened nfl game_winner 2026-09-20 CAR@ATL CAR: bought 20 of 50 on kalshi, 30 exposed, 30 still exposed, hedge +1.60$"
    assert ex.exposed == {}
    assert cash["kalshi"] == pytest.approx(10000 - 20 * 0.47)


def test_a_settled_trade_is_not_flattened_any_more(tmp_path, quick):
    from db.models import Contract
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time="2026-09-20T21:00:00+00:00", fee_info=None) for v, c in (("kalshi", "k"), ("polymarket_us", "pm"))], NOW)
    conn.execute("INSERT INTO pairs (id, label, kind, venues, contracts, flags, matched_at) VALUES (1, ?, 'game_winner', '', 2, '[]', ?)", (PAIR["label"], NOW))

    def kalshi_vanishes_and_polymarket_loses_its_bids():
        latest.pop(("kalshi", "k"))
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])

    run(ex, kalshi_vanishes_and_polymarket_loses_its_bids)
    assert list(ex.exposed) == [stored(conn)[0]["id"]]                  # 50 yes held, with nothing to flatten against.
    s = settle.Settler(conn, cash, lambda m: None, executor=ex)
    s.results = {"polymarket_us": lambda events: {"pm": ("yes", "2026-09-20T17:40:00+00:00")}}
    latest.update(books())                                              # Kalshi is back and could flatten the rest.

    async def later():
        await s.settle("2026-09-20T17:45:00+00:00")                     # The contract was decided during the game and has settled.
        ex.tick("2026-09-20T17:46:00+00:00")
        await asyncio.gather(*ex.tasks)
    asyncio.run(later())
    t = stored(conn)[0]
    settlement = conn.execute("SELECT yes_payout, settled_at FROM settlements WHERE trade_id = ?", (t["id"],)).fetchone()
    assert ex.exposed == {} and ex.tasks == set()
    assert (t["yes_held"], t["no_held"], *settlement) == (50, 0, 50, "2026-09-20T17:40:00+00:00")
    assert [r[0] for r in conn.execute("SELECT reason FROM ledger ORDER BY id")][2:] == ["buy", "payout"]     # No sale after the payout.


def test_a_stale_book_is_not_flattened_against(tmp_path, quick):
    latest = books()
    clock = [NOW]
    conn, cash, ex = executor(tmp_path, latest, clock=lambda: clock[0])

    def kalshi_vanishes_and_polymarket_loses_its_bids():
        latest.pop(("kalshi", "k"))
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])

    run(ex, kalshi_vanishes_and_polymarket_loses_its_bids)
    assert list(ex.exposed) == [stored(conn)[0]["id"]]              # 50 yes held, no book to flatten against.

    async def at(now):
        clock[0] = now
        ex.tick(now)
        await asyncio.gather(*ex.tasks)

    # Kalshi's book comes back from 17:30, but by 17:32 it has not changed for two minutes, as a closed market's would not.
    latest.update({("kalshi", "k"): books()[("kalshi", "k")]})
    asyncio.run(at("2026-09-20T17:32:00+00:00"))
    assert stored(conn)[0]["no_held"] == 0 and list(ex.exposed) == [stored(conn)[0]["id"]]
    # Once the book changes again it is fresh, and the rest is flattened against it.
    latest[("kalshi", "k")] = Book("kalshi", "k", "2026-09-20T17:32:00+00:00", [[0.53, 100]], [[0.54, 100]])
    asyncio.run(at("2026-09-20T17:32:00+00:00"))
    t = stored(conn)[0]
    assert (t["yes_held"], t["no_held"], t["matched"]) == (50, 50, 50) and ex.exposed == {}


def test_rejected_orders_fail_without_a_hedge(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(config, "REJECT_PROBABILITY", 1)
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    run(ex)
    t = stored(conn)[0]
    assert (t["status"], t["matched"], t["hedge"]) == ("failed", 0, "yes leg rejected, no leg rejected")
    assert cash.amounts == pytest.approx({"polymarket_us": 10000, "kalshi": 10000})


def test_signal_is_refused_for_thin_edges_and_games_not_in_play(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    assert ex.signal(PAIR, YES, NO, 0.015, 100, FEES, NOW) is False
    future = dict(YES, start_time=None, close_time="2027-02-14T00:00:00+00:00")
    assert ex.signal(PAIR, future, dict(NO, start_time=None, close_time="2027-02-14T00:00:00+00:00"), 0.05, 100, FEES, NOW) is False   # A future.
    assert ex.signal(PAIR, YES, NO, 0.08, 100, FEES, "2026-09-20T16:59:00+00:00") is False        # Before kickoff.
    assert ex.signal(PAIR, YES, NO, 0.08, 100, FEES, "2026-09-20T20:16:00+00:00") is False        # After the final whistle.
    assert ex.tasks == set() and stored(conn) == []


class HalfHour:
    """
    A stand in for the allocator: whether the game is in play, the cap, and what is left of the half hour's budget.
    """

    def __init__(self, playing=True, cap=1000, room=None):
        self.playing, self.limit, self.left = playing, cap, room or {}

    def in_play(self, pair, now):
        return self.playing

    def cap(self, pair, now):
        return self.limit

    def room(self, now):
        return self.left


def test_a_trade_keeps_to_the_half_hours_budget_and_the_scoreboards_word_on_the_game(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    ex.allocator = HalfHour(room={"polymarket_us": 9.2, "kalshi": 100.0})
    assert run(ex) == [True] and stored(conn)[0]["quantity"] == 20            # 9.20 dollars at 0.45 buys 20.
    ex.allocator = HalfHour(playing=False)
    assert run(ex) == [False]                                                   # The venue has called the game over.

    # Past its expected end, a game the scoreboard says is still live is traded, and pays no sooner than half an hour on.
    async def late():
        sent = ex.signal(PAIR, YES, NO, 0.08, 100, FEES, "2026-09-20T20:40:00+00:00")
        await asyncio.gather(*ex.tasks)
        return sent
    ex.allocator = HalfHour(room={"polymarket_us": 100.0, "kalshi": 100.0})
    assert asyncio.run(late()) is True and stored(conn)[-1]["pays_at"] == "2026-09-20T21:10:00+00:00"


def test_two_signals_at_once_share_the_balance_instead_of_both_spending_it(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest, start=530.0)     # 30 over the 500 floor: room for 50 contracts at 0.45 once, not twice.
    assert run(ex, signals=2) == [True, True]
    first, second = stored(conn)
    # The second saw what the first had reserved on both venues: 7.5 left on Polymarket US and 6.5 on Kalshi, so 13 at 0.47.
    assert (first["quantity"], second["quantity"]) == (50, 13)
    assert cash.amounts == pytest.approx({"polymarket_us": 530 - 63 * 0.45, "kalshi": 530 - 63 * 0.47})
    assert min(cash.amounts.values()) >= 500


def test_new_paper_trades_leave_500_dollars_on_each_venue(tmp_path, quick):
    latest = books()
    logs = []
    conn, cash, ex = executor(tmp_path, latest, logs.append, start=510.0)
    assert run(ex) == [True] and stored(conn)[0]["quantity"] == 21      # 10 to spend: 22 at 0.45 but 21 at 0.47.
    assert cash.amounts == pytest.approx({"polymarket_us": 510 - 21 * 0.45, "kalshi": 510 - 21 * 0.47})
    ex.tick(NOW)
    assert not any("floor" in line for line in logs)                    # Both still at 500 or more.
    cash.amounts["kalshi"] = 499.0
    ex.tick(NOW)
    assert logs[-1] == "paper kalshi has 499.00$, under its 500.00$ floor, so new trades wait until more arrives"
    assert run(ex) == [False]
    cash.amounts["kalshi"] = 600.0
    ex.tick(NOW)
    assert logs[-1] == "paper kalshi has 600.00$, back over its 500.00$ floor"


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
    database.replace_pairs(conn, "nfl", [Pair(PAIR["label"], "game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, bets, [], sport="nfl")], NOW)
    calls = []
    s = scan.Scanner(conn, ("nfl",), lambda m: None,
                     on_signals=[lambda pair, yes, no, edge, size, fee_infos, now: calls.append((pair["label"], round(edge, 2), now)) or True])
    latest = books()
    s.on_book("polymarket_us", "pm", latest, NOW)
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.40, 100]], [[0.41, 100]])
    s.on_book("polymarket_us", "pm", latest, "2026-09-20T17:30:01+00:00")     # A bigger edge in the same episode brings no second signal.
    assert calls == [(PAIR["label"], 0.08, NOW)]


def test_a_paper_executor_refuses_money_of_another_mode(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.mode = "live"
    with pytest.raises(ValueError, match="a paper executor cannot trade live money"):
        PaperExecutor(conn, cash, lambda: {})


def test_each_executor_is_offered_the_episode_until_it_takes_a_trade(tmp_path):
    from db.models import Bet, Contract, Pair
    conn = database.connect(tmp_path / "t.sqlite")
    members = [("kalshi", "k", NO_K_FEES), ("polymarket_us", "pm", NO_PM_FEES)]
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time="2026-09-20T21:00:00+00:00", fee_info=f) for v, c, f in members], NOW)
    bets = [Bet(v, c, "game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, "yes") for v, c, _ in members]
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", [Pair(PAIR["label"], "game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, bets, [], sport="nfl")], NOW)
    live, paper = [], []
    busy = [True]           # The live executor turns the first moment down, say while its balance has not been read.
    s = scan.Scanner(conn, ("nfl",), lambda m: None, on_signals=[lambda *args: live.append(args[-1]) or not busy[0],
                                                             lambda *args: paper.append(args[-1]) or True])
    latest = books()
    s.on_book("polymarket_us", "pm", latest, NOW)
    busy[0] = False
    s.on_book("polymarket_us", "pm", latest, "2026-09-20T17:30:01+00:00")
    s.on_book("polymarket_us", "pm", latest, "2026-09-20T17:30:02+00:00")
    assert live == [NOW, "2026-09-20T17:30:01+00:00"] and paper == [NOW]
