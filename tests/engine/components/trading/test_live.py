"""
Tests for the live executor, with the venues' answers to its orders scripted by each test.
"""

import asyncio
import threading
import pytest
from api import orders
from db import database
from db.models import Book, Order
from engine.components.money.live import LiveBalances
from engine.components.money.paper import PaperBalances
from engine.components.trading.live import LiveExecutor
from engine.components.trading.paper import PaperExecutor
from engine.helper import config
from trade_setup import FEES, NO, NO_K_FEES, NO_PM_FEES, NOW, PAIR, YES, stored
from trade_setup import books as shared_books


def fills(n=None):
    """
    A scripted answer that fills n contracts at the limit, or the whole order when n is None, with no fees.
    """
    def answer(quantity, price):
        got = quantity if n is None else min(n, quantity)
        return orders.Answer(f"venue-{got}", orders.status(got, quantity), got, got * price, 0.0, None, {"filled": got})
    return answer


REFUSED = orders.Answer(None, "rejected", 0, 0.0, 0.0, "insufficient balance", {})
UNKNOWN = orders.unknown(TimeoutError("timed out"))


class Venues:
    """
    Stands in for the venues' place_order: each venue answers from its own script, and every order is recorded.
    """

    def __init__(self, **scripts):
        self.scripts = {"kalshi": [], "polymarket_us": [], **scripts}
        self.orders = []

    def place(self):
        def for_venue(venue):
            def place_order(contract_id, action, outcome, quantity, price, client_id):
                self.orders.append((venue, action, outcome, quantity, price))
                answer = self.scripts[venue].pop(0)
                return answer(quantity, price) if callable(answer) else answer
            return place_order
        return {venue: for_venue(venue) for venue in self.scripts}


def books(size=20, **prices):
    """
    The shared books with 20 contracts a level, so the trades here ask for half of them, 10 contracts.
    """
    return shared_books(size=size, **prices)


class FakeNotifier:
    """
    Stands in for the Notifier, keeping what it was asked to send as (kind, subject, body).
    """

    def __init__(self):
        self.sent = []

    def send(self, kind, subject, body, now=None):
        self.sent.append((kind, subject, body))


def executor(tmp_path, venues, latest=None, notifier=None, logs=None, read=True, balance=1000.0, positions=None):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: (balance, {}), "polymarket_us": lambda: (balance, {})})
    if read:
        asyncio.run(cash.refresh(NOW))
    latest = books() if latest is None else latest
    ex = LiveExecutor(conn, cash, lambda: latest, (logs.append if logs is not None else lambda m: None), clock=lambda: NOW,
                      place=venues.place(), notifier=notifier, positions=positions or {"polymarket_us": lambda: {}})
    return conn, cash, ex


def trade(ex, times=1):
    """
    Send the signal inside a loop as many times as asked, one trade after the other, and wait for each to finish.
    """
    async def scenario():
        sent = []
        for _ in range(times):
            sent.append(ex.signal(PAIR, YES, NO, 1 - 0.45 - 0.47, 100, FEES, NOW))
            while ex.tasks:
                await asyncio.gather(*ex.tasks)
        return sent
    return asyncio.run(scenario())


def test_both_legs_are_sent_as_real_orders_and_every_order_is_stored(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, cash, ex = executor(tmp_path, venues, books(size=100))
    assert trade(ex) == [True]
    t = stored(conn, "trades")[0]
    # Half the visible 100, with no cap, as on paper.
    assert (t["mode"], t["status"], t["quantity"], t["yes_filled"], t["no_filled"], t["matched"]) == ("live", "filled", 50, 50, 50, 50)
    assert t["profit"] == pytest.approx(50 * (1 - 0.45 - 0.47))
    # Yes is the Polymarket US contract itself. No is the other side of the Kalshi contract, bought at no more than 1 - 0.53.
    assert sorted(venues.orders) == [("kalshi", "buy", "no", 50, 0.47), ("polymarket_us", "buy", "yes", 50, 0.45)]
    stored_orders = stored(conn, "orders")
    assert {(o["trade_id"], o["purpose"], o["status"], o["filled"], o["venue_order_id"]) for o in stored_orders} == {(t["id"], "open", "filled", 50, "venue-50")}
    assert all(o["client_id"] and o["answered_at"] == NOW and o["response"] == '{"filled": 50}' for o in stored_orders)
    assert cash.amounts == pytest.approx({"polymarket_us": 1000 - 22.5, "kalshi": 1000 - 23.5})
    assert stored(conn, "ledger") == []                                 # Live money keeps no ledger of ours.


def test_a_leg_that_filled_short_is_evened_by_selling_the_other_back_no_lower_than_the_books_said(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[fills(4)])
    logs = []
    conn, cash, ex = executor(tmp_path, venues, logs=logs)
    trade(ex)
    t = stored(conn, "trades")[0]
    # Buying the 6 missing on Kalshi at 0.47 would earn 8 cents each but hold the money until the game pays, so the 6 extra yes
    # are sold back at the 0.44 bid, a cent under what they cost.
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 6, 0.44)
    assert (t["yes_held"], t["no_held"], t["matched"], t["status"], t["hedge"]) == (4, 4, 4, "partial", "sold back 6 of 6 on polymarket_us")
    assert [(o["purpose"], o["quantity"], o["limit_price"]) for o in stored(conn, "orders")][-1] == ("flatten", 6, 0.44)
    assert logs[-1].startswith("live partial: nfl game_winner 2026-09-22 CAR@ATL CAR")


def bids_gone(latest):
    """
    A scripted Kalshi answer: its bids were taken before our order arrived, so nothing filled and the book shows none.
    """
    def answer(quantity, price):
        latest[("kalshi", "k")] = Book("kalshi", "k", NOW, [], [[0.54, 100]])
        return fills(0)(quantity, price)
    return answer


def test_a_sale_back_is_sent_no_lower_than_the_books_said(tmp_path):
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)
    t = stored(conn, "trades")[0]
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 10, 0.44)
    assert (t["yes_held"], t["no_held"], t["hedge"]) == (0, 0, "sold back 10 of 10 on polymarket_us")
    assert t["hedge_pnl"] == pytest.approx(10 * (0.44 - 0.45))


def test_an_order_of_unknown_outcome_sets_its_trade_aside_and_trading_goes_on(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[UNKNOWN, fills()])
    notifier, logs = FakeNotifier(), []
    conn, cash, ex = executor(tmp_path, venues, notifier=notifier, logs=logs)
    trade(ex)
    assert len(venues.orders) == 2                                      # Nothing is sent to flatten, since what the trade holds is unknown.
    t = stored(conn, "trades")[0]
    kalshi_order = next(o for o in stored(conn, "orders") if o["venue"] == "kalshi")
    assert (kalshi_order["status"], kalshi_order["filled"]) == ("error", 0)
    assert (t["yes_held"], t["no_held"]) == (10, 0) and ex.exposed == {}
    assert t["hedge"] == f"10 exposed, set aside, order {kalshi_order['id']} has an unknown outcome, no leg error: TimeoutError('timed out')"
    assert notifier.sent == []                                         # Only a halt is emailed.
    (line,) = [line for line in logs if line.startswith(f"live trade {t['id']} set aside")]
    assert f"order {kalshi_order['id']}, buy 10 no of kalshi k" in line and f"client id {kalshi_order['client_id']}" in line
    assert ex.halted is None and trade(ex) == [True]                    # The next trade goes ahead.
    assert stored(conn, "trades")[1]["status"] == "filled"
    assert executor(tmp_path, Venues())[2].exposed == {}                # A restart leaves it to a human too.


def test_live_trading_halts_at_three_unknown_outcomes_in_twenty_orders(tmp_path):
    venues = Venues(polymarket_us=[UNKNOWN, fills(), fills(), fills(), UNKNOWN] + [fills()] * 5, kalshi=[fills(), fills(), UNKNOWN] + [fills()] * 7)
    notifier = FakeNotifier()
    conn, cash, ex = executor(tmp_path, venues, notifier=notifier)
    assert trade(ex, 2) == [True, True] and ex.halted is None           # One unknown in four orders.
    assert trade(ex, 2) == [True, True] and ex.halted is None           # Two in eight, not in a row.
    assert trade(ex, 1) == [True] and ex.halted.startswith("3 of the last ")      # The third, in the tenth order, halts.
    unknown = ", ".join(str(o["id"]) for o in stored(conn, "orders") if o["status"] == "error")
    assert f"orders had an unknown outcome, orders {unknown}, the last on " in ex.halted and trade(ex) == [False]
    assert ex.brakes.stopped == ex.halted                               # Flattening stops too.
    assert [kind for kind, _, _ in notifier.sent] == ["halt"]           # One email, naming every order to look up.


def test_a_venue_refusing_orders_in_a_row_halts_live_trading(tmp_path):
    venues = Venues(polymarket_us=[REFUSED, fills(0), REFUSED, REFUSED], kalshi=[REFUSED, REFUSED, REFUSED, REFUSED])
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex, 2) == [True, True] and not ex.halted               # An order Polymarket US took, though it filled nothing, starts it over.
    assert trade(ex, 2) == [True, False]
    assert ex.halted == "kalshi refused 3 orders in a row, the last with: insufficient balance"
    assert ex.brakes.stopped == ex.halted                               # Flattening stops too.


def test_flattening_losses_over_the_limit_halt_live_trading(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_LOSS_SHARE", 0.00004)          # A tenth of a dollar lost on 2,000 is 0.005%.
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)                                                           # Sold back at a cent under cost, 10 cents in all, and flat.
    assert ex.halted == "the live trades decided in the last 6 hours lost 0.10$, 0% of the live money, over the 0% limit"
    assert ex.brakes.stopped is None                                    # Flattening goes on.


def test_after_a_loss_halt_flattening_goes_on_until_its_orders_fail(tmp_path):
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills(0), fills(4)] + [REFUSED] * 3, kalshi=[bids_gone(latest)])
    notifier = FakeNotifier()
    conn, cash, ex = executor(tmp_path, venues, latest, notifier=notifier)
    trade(ex)                                                           # Kalshi filled nothing, and the sale back found nothing.
    assert list(ex.exposed) == [1]
    ex.brakes.halt("the live trades decided in the last 6 hours lost too much")
    assert trade(ex) == [False] and len(venues.orders) == 3             # No new trades.
    assert "; HALTED, still flattening: the live trades decided" in ex.summary()
    ex.clock = ex.brakes.clock = lambda: "2026-09-20T17:30:01+00:00"    # A second on.
    asyncio.run(ex.retry(ex.clock()))
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 10, 0.44) and stored(conn, "trades")[0]["yes_held"] == 6
    for _ in range(3):
        asyncio.run(ex.retry(ex.clock()))                               # Refused each time.
    assert ex.brakes.stopped == "polymarket_us refused 3 orders in a row, the last with: insufficient balance"
    asyncio.run(ex.retry(ex.clock()))
    assert len(venues.orders) == 7 and list(ex.exposed) == [1]          # Nothing more is sent.
    assert "; HALTED, no orders at all: polymarket_us refused 3 orders in a row" in ex.summary()
    (_, first, halted), (_, second, stopped) = notifier.sent
    assert (first, second) == ("SportsArb live trading halted", "SportsArb live flattening stopped")
    assert "Exposed trades are still flattened." in halted and "No more orders are sent, flattening included." in stopped


def test_nothing_is_traded_before_the_first_balance_reading(tmp_path):
    venues = Venues()
    conn, cash, ex = executor(tmp_path, venues, read=False)
    assert trade(ex) == [False] and venues.orders == []


def test_each_executor_trades_only_its_own_money(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    with pytest.raises(ValueError, match="a live executor cannot trade paper money"):
        LiveExecutor(conn, PaperBalances(conn), lambda: {})
    with pytest.raises(ValueError, match="a paper executor cannot trade live money"):
        PaperExecutor(conn, LiveBalances(), lambda: {})


def test_an_answer_that_cannot_be_read_counts_as_an_unknown_outcome(tmp_path):
    def unreadable(quantity, price):
        raise KeyError("executions")
    venues = Venues(polymarket_us=[fills()], kalshi=[unreadable])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)
    assert "KeyError('executions')" in ex.set_aside[1] or "KeyError('executions')" in stored(conn, "orders")[1]["note"]
    assert 1 in ex.set_aside and ex.halted is None
    assert cash.reserved == {"kalshi": 0, "polymarket_us": 0}          # The reservation came back all the same.


def test_a_halt_outlasts_a_restart_until_a_human_removes_the_file_and_then_starts_over(tmp_path, halt_file):
    from engine.components.trading import notify
    conn = database.connect(tmp_path / "t.sqlite")
    notifier = notify.Notifier(conn, lambda m: None, sender=lambda *args: None)     # Halts are stored as alerts, as the session wires them.
    no_bids = {**books(), ("polymarket_us", "pm"): Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])}     # Nothing to sell into.
    venues = Venues(polymarket_us=[fills()] * 3, kalshi=[REFUSED] * 3)
    conn, cash, ex = executor(tmp_path, venues, no_bids, notifier=notifier)
    trade(ex, 3)
    assert halt_file.read_text().startswith("2026-09-20T17:30:00 UTC every order stopped: kalshi refused 3 orders in a row")
    logs = []
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[REFUSED, fills()])
    conn, cash, again = executor(tmp_path, venues, logs=logs)           # A crash or a deploy restarts the process.
    assert again.halted.startswith(f"halted before this start, remove {halt_file} to resume: ")
    assert logs[0] == "live trades left exposed before this start, flattening again: 1, 2, 3"
    assert logs[1].startswith("live trading halted before this start") and trade(again) == [False] and venues.orders == []
    halt_file.unlink()                                                  # Checked and cleared by a human.
    conn, cash, resumed = executor(tmp_path, venues)
    assert resumed.halted is None and trade(resumed) == [True]
    assert resumed.halted is None                                       # Its refusal is the first since the halt, not the fourth in a row.


def test_a_restart_takes_back_what_was_left_exposed_and_flattens_it_again(tmp_path):
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills(0)], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)                                                           # Kalshi filled nothing, and the sale back found nothing.
    ex.brakes.halt("the live trades decided in the last 6 hours lost too much")
    logs = []
    venues = Venues(polymarket_us=[fills()])
    conn, cash, again = executor(tmp_path, venues, latest, logs=logs)   # A crash or a deploy restarts the process.
    assert list(again.exposed) == [1] and "live trades left exposed before this start, flattening again: 1" in logs
    assert again.halted and again.brakes.stopped is None                # Still halted on results, so still flattening.
    asyncio.run(again.retry(NOW))
    assert venues.orders == [("polymarket_us", "sell", "yes", 10, 0.44)]
    assert again.exposed == {} and stored(conn, "trades")[0]["yes_held"] == 0


def test_orders_the_latency_stopgap_turned_away_do_not_count_as_refusals(tmp_path):
    stopgap = orders.Answer(None, "unfilled", 0, 0.0, 0.0, "latency stopgap: Global Rate Limit Exceeded", {})
    venues = Venues(polymarket_us=[stopgap] * 4, kalshi=[fills(0)] * 4)
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex, 4) == [True] * 4 and ex.halted is None
    assert "yes leg unfilled: latency stopgap" in stored(conn, "trades")[0]["hedge"]


def test_trades_spend_all_the_cash_and_a_venue_or_kalshi_shard_running_low_emails_once(tmp_path):
    shards = {0: 8.0, 3: 20.0}                          # Football's shard and baseball's.
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: (sum(shards.values()), dict(shards)), "polymarket_us": lambda: (8.0, {})})
    asyncio.run(cash.refresh(NOW))
    notifier, logs = FakeNotifier(), []
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn = database.connect(tmp_path / "t.sqlite")
    ex = LiveExecutor(conn, cash, lambda: books(size=100), logs.append, clock=lambda: NOW, place=venues.place(), notifier=notifier)
    football = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): dict(NO_K_FEES, exchange_index=0)}

    async def scenario():
        sent = ex.signal(PAIR, YES, NO, 1 - 0.45 - 0.47, 100, football, NOW)
        await asyncio.gather(*ex.tasks)
        return sent
    # All 8 dollars on Polymarket US and on Kalshi's shard 0 may be spent: half the 100 shown is 50, but the 8 dollars pay for
    # 17 contracts at 0.45 or at 0.47.
    assert cash.spendable("kalshi", 0) == pytest.approx(8.0)
    assert asyncio.run(scenario()) is True and stored(conn, "trades")[0]["quantity"] == 17
    ex.tick(NOW)
    ex.tick(NOW)
    # Once each: Kalshi's shard 0, whose 8 dollars less the 7.99 bought is 0.01, and Polymarket US, 8 less 7.65. Shard 3 has plenty.
    assert [(kind, subject) for kind, subject, _ in notifier.sent] == [("low_cash", "SportsArb live kalshi shard 0 cash low: 0.01$"),
                                                                        ("low_cash", "SportsArb live polymarket_us cash low: 0.35$")]
    assert "move some to the shard with python3 -m tools.kalshi_shards" in notifier.sent[0][2]
    cash.shard_read[("kalshi", 0)] = 20.0                               # A payout arrives.
    ex.tick(NOW)
    assert logs[-1] == "live kalshi shard 0 has 12.01$, back over 5.00$"
    cash.shard_read[("kalshi", 0)] = 4.0
    cash.shard_read[("kalshi", 3)] = 1.0
    ex.tick(NOW)
    assert [subject for _, subject, _ in notifier.sent][-2:] == ["SportsArb live kalshi shard 0 cash low: -3.99$",     # 4 read, less 7.99.
                                                                  "SportsArb live kalshi shard 3 cash low: 1.00$"]


def test_both_opening_orders_go_out_at_once(tmp_path):
    kalshi_sent = threading.Event()

    def polymarket_us(quantity, price):
        # Answers only once Kalshi has its order too, which it would not have yet if Kalshi went second.
        return fills()(quantity, price) if kalshi_sent.wait(timeout=2) else REFUSED

    def kalshi(quantity, price):
        kalshi_sent.set()
        return fills()(quantity, price)
    venues = Venues(polymarket_us=[polymarket_us], kalshi=[kalshi])
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex) == [True]
    t = stored(conn, "trades")[0]
    assert (t["yes_filled"], t["no_filled"], t["status"]) == (10, 10, "filled")


def test_a_kalshi_leg_trades_only_with_the_cash_on_its_markets_shard(tmp_path):
    shards = {0: 1000.0, 3: 0.0}                        # Kalshi's cash all on football's shard, none on baseball's.
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: (sum(shards.values()), dict(shards)), "polymarket_us": lambda: (1000.0, {})})
    asyncio.run(cash.refresh(NOW))
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn = database.connect(tmp_path / "t.sqlite")
    ex = LiveExecutor(conn, cash, lambda: books(), lambda m: None, clock=lambda: NOW, place=venues.place())
    baseball = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): dict(NO_K_FEES, exchange_index=3)}

    def signal():
        async def scenario():
            sent = ex.signal(dict(PAIR, sport="mlb"), YES, NO, 1 - 0.45 - 0.47, 100, baseball, NOW)
            await asyncio.gather(*ex.tasks)
            return sent
        return asyncio.run(scenario())

    assert signal() is False and venues.orders == []
    shards[3] = 3 * 0.47 + 0.01                     # Room for three contracts on baseball's shard.
    asyncio.run(cash.refresh(NOW))
    assert signal() is True
    assert sorted(venues.orders) == [("kalshi", "buy", "no", 3, 0.47), ("polymarket_us", "buy", "yes", 3, 0.45)]     # Sent together.
    assert cash.available("kalshi", 3) == pytest.approx(0.01) and cash.available("kalshi", 0) == 1000.0


def test_a_leg_filled_in_hundredths_is_flattened_to_the_hundredth(tmp_path):
    venues = Venues(polymarket_us=[fills(6.42)], kalshi=[fills(), fills()])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)
    t = stored(conn, "trades")[0]
    # Polymarket US filled 6.42 of the 10, so the 3.58 no over on Kalshi are sold back, at 1 - 0.54, to the hundredth.
    assert venues.orders[-1] == ("kalshi", "sell", "no", 3.58, 0.46)
    assert (t["yes_filled"], t["no_filled"], t["matched"], t["yes_held"], t["no_held"], t["status"]) == (6.42, 10, 6.42, 6.42, 6.42, "partial")
    assert t["profit"] == pytest.approx(6.42 * (1 - 0.45 - 0.47)) and t["hedge"] == "sold back 3.58 of 3.58 on kalshi"
    assert t["hedge_pnl"] == pytest.approx(3.58 * (0.46 - 0.47))
    assert ex.exposed == {}


def check(ex):
    asyncio.run(ex.check_positions())


def test_a_venue_holding_other_than_the_trades_is_logged_once(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    held, logs = {"pm": 10.0}, []           # The trade holds 10 yes of pm, as the venue does.
    conn, cash, ex = executor(tmp_path, venues, logs=logs, positions={"polymarket_us": lambda: held, "kalshi": lambda: {"k": -10.0}})
    trade(ex)
    check(ex)
    assert not any("differ" in line for line in logs)
    held.update(pm=10.42, other=-1.0)
    check(ex)
    check(ex)
    assert [line for line in logs if "differ" in line] == [
        "live polymarket_us holds -1 of other, the trades 0: the records differ from the venue, see tools/repair_fills.py",
        "live polymarket_us holds 10.42 of pm, the trades 10: the records differ from the venue, see tools/repair_fills.py"]
    assert len(venues.orders) == 2                                      # Only logged, nothing sent.


def test_positions_are_not_compared_while_orders_go_out(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    logs = []
    conn, cash, ex = executor(tmp_path, venues, logs=logs)
    trade(ex)

    def read_while_a_trade_goes_out():                                  # In a thread of its own, so on a connection of its own.
        database.insert_order(database.connect(tmp_path / "t.sqlite"), Order(
            trade_id=1, venue="kalshi", contract_id="k", purpose="open", action="buy", outcome="no", quantity=1, limit_price=0.47,
            client_id="c", sent_at=NOW))
        return {"pm": 12.0}
    ex.positions = {"polymarket_us": read_while_a_trade_goes_out}
    check(ex)
    ex.positions = {"polymarket_us": lambda: {"pm": 12.0}}
    ex.tasks.add(asyncio.Future)                                        # A trade in flight.
    check(ex)
    assert not any("differ" in line for line in logs)
