"""
Tests for the live executor, with the venues' answers to its orders scripted by each test.
"""

import asyncio
import pytest
from api import orders
from db import database
from db.models import Quote
from live.components import accounts, balances
from live.components.execute import live
from live.components.execute.live import LiveExecutor
from live.components.execute.paper import PaperExecutor
from live.helper import config

NO_PM_FEES = {"feeCoefficient": 0}
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}
KICKOFF = "2026-09-27T17:00:00+00:00"
NOW = "2026-09-27T17:30:00+00:00"
PAIR = {"id": 1, "label": "game_winner 2026-09-27 CAR@ATL CAR", "kind": "game_winner"}
YES = {"venue": "polymarket_us", "contract_id": "pm", "polarity": "yes", "start_time": KICKOFF, "close_time": "2026-09-27T21:00:00+00:00"}
NO = {"venue": "kalshi", "contract_id": "k", "polarity": "yes", "start_time": KICKOFF, "close_time": "2026-09-27T21:00:00+00:00"}
FEES = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES}


def books():
    """
    Yes is cheapest on Polymarket US at the 0.45 ask and no on Kalshi at 0.47 through the 0.53 bid, 100 deep on every level.
    """
    return {("polymarket_us", "pm"): Quote("polymarket_us", "pm", NOW, [[0.44, 100]], [[0.45, 100]]),
            ("kalshi", "k"): Quote("kalshi", "k", NOW, [[0.53, 100]], [[0.54, 100]])}


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


@pytest.fixture(autouse=True)
def halt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "HALT_FILE", tmp_path / "live_halted.txt")
    return live.HALT_FILE


def executor(tmp_path, venues, latest=None, alerts=None, logs=None, read=True):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = accounts.Accounts(lambda m: None, {"kalshi": lambda: 1000.0, "polymarket_us": lambda: 1000.0})
    if read:
        asyncio.run(cash.refresh(NOW))
    latest = books() if latest is None else latest
    alert = (lambda subject, body: alerts.append((subject, body))) if alerts is not None else None
    ex = LiveExecutor(conn, cash, lambda: latest, (logs.append if logs is not None else lambda m: None), clock=lambda: NOW,
                      place=venues.place(), alert=alert)
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


def stored(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_both_legs_are_sent_as_real_orders_and_every_order_is_stored(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex) == [True]
    t = stored(conn, "trades")[0]
    # The live cap, not half the visible 100, sets the size.
    assert (t["mode"], t["status"], t["quantity"], t["yes_filled"], t["no_filled"], t["matched"]) == ("live", "filled", 10, 10, 10, 10)
    assert t["profit"] == pytest.approx(10 * (1 - 0.45 - 0.47))
    # Yes is the Polymarket US contract itself. No is the other side of the Kalshi contract, bought at no more than 1 - 0.53.
    assert sorted(venues.orders) == [("kalshi", "buy", "no", 10, 0.47), ("polymarket_us", "buy", "yes", 10, 0.45)]
    stored_orders = stored(conn, "orders")
    assert {(o["trade_id"], o["purpose"], o["status"], o["filled"], o["venue_order_id"]) for o in stored_orders} == {(t["id"], "open", "filled", 10, "venue-10")}
    assert all(o["client_id"] and o["answered_at"] == NOW and o["response"] == '{"filled": 10}' for o in stored_orders)
    assert cash.amounts == pytest.approx({"polymarket_us": 1000 - 4.5, "kalshi": 1000 - 4.7})
    assert stored(conn, "ledger") == []                                 # Live money keeps no ledger of ours.


def test_a_leg_that_filled_short_is_flattened_no_higher_than_the_books_said(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills(4), fills()])
    logs = []
    conn, cash, ex = executor(tmp_path, venues, logs=logs)
    trade(ex)
    t = stored(conn, "trades")[0]
    # Buying the 6 missing on Kalshi at 0.47 earns 8 cents each, selling 6 back at 0.44 loses one, so the rest is bought.
    assert venues.orders[-1] == ("kalshi", "buy", "no", 6, 0.47)
    assert (t["yes_held"], t["no_held"], t["matched"], t["status"], t["hedge"]) == (10, 10, 10, "filled", "bought 6 of 6 on kalshi")
    assert [(o["purpose"], o["quantity"], o["limit_price"]) for o in stored(conn, "orders")][-1] == ("flatten", 6, 0.47)
    assert logs[-1].startswith("live filled: game_winner 2026-09-27 CAR@ATL CAR")


def bids_gone(latest):
    """
    A scripted Kalshi answer: its bids were taken before our order arrived, so nothing filled and the book shows none.
    """
    def answer(quantity, price):
        latest[("kalshi", "k")] = Quote("kalshi", "k", NOW, [], [[0.54, 100]])
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


def test_an_order_of_unknown_fate_halts_live_trading_and_tells_a_human(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[UNKNOWN])
    alerts, logs = [], []
    conn, cash, ex = executor(tmp_path, venues, alerts=alerts, logs=logs)
    trade(ex)
    assert len(venues.orders) == 2                                      # Nothing is sent to flatten, since what is held is unknown.
    t = stored(conn, "trades")[0]
    assert (t["yes_held"], t["no_held"]) == (10, 0)
    assert "10 exposed" in t["hedge"] and "no leg error: TimeoutError('timed out')" in t["hedge"]
    kalshi_order = next(o for o in stored(conn, "orders") if o["venue"] == "kalshi")
    assert (kalshi_order["status"], kalshi_order["filled"]) == ("error", 0)
    assert ex.halted.startswith(f"order {kalshi_order['id']} for trade {t['id']} (buy 10 no of kalshi k) has an unknown fate")
    assert kalshi_order["client_id"] in ex.halted
    assert [subject for subject, _ in alerts] == ["SportsArb live trading halted"] and "remove" in alerts[0][1]
    assert any(line.startswith("live trading halted: ") for line in logs)
    assert trade(ex) == [False] and len(venues.orders) == 2             # No new trades either.
    assert "HALTED" in ex.summary()


def test_a_venue_refusing_orders_in_a_row_halts_live_trading(tmp_path):
    venues = Venues(polymarket_us=[REFUSED, fills(0), REFUSED, REFUSED], kalshi=[REFUSED, REFUSED, REFUSED, REFUSED])
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex, 2) == [True, True] and not ex.halted
    assert ex.rejects == {"polymarket_us": 0, "kalshi": 2}              # An order Polymarket US took, though it filled nothing, starts it over.
    assert trade(ex, 2) == [True, False]
    assert ex.halted == "kalshi refused 3 orders in a row, the last with: insufficient balance"


def test_flattening_losses_over_the_limit_halt_live_trading(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_HEDGE_LOSS", 0.05)
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)                                                           # Sold back at a cent under cost, 10 cents in all.
    assert ex.halted == "flattening has lost 0.10$ since the start, over the limit of 0.05$"


def test_nothing_is_traded_before_the_first_balance_reading(tmp_path):
    venues = Venues()
    conn, cash, ex = executor(tmp_path, venues, read=False)
    assert trade(ex) == [False] and venues.orders == []


def test_each_executor_trades_only_its_own_money(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    with pytest.raises(ValueError, match="a live executor cannot trade paper money"):
        LiveExecutor(conn, balances.Balances(conn), lambda: {})
    with pytest.raises(ValueError, match="a paper executor cannot trade live money"):
        PaperExecutor(conn, accounts.Accounts(), lambda: {})


def test_an_answer_that_cannot_be_read_counts_as_an_unknown_fate(tmp_path):
    def unreadable(quantity, price):
        raise KeyError("executions")
    venues = Venues(polymarket_us=[fills()], kalshi=[unreadable])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)
    assert ex.halted and "KeyError('executions')" in ex.halted
    assert cash.reserved == {"kalshi": 0, "polymarket_us": 0}          # The reservation came back all the same.


def test_a_halt_outlasts_a_restart_until_a_human_removes_the_file(tmp_path, halt_file):
    conn, cash, ex = executor(tmp_path, Venues(polymarket_us=[fills()], kalshi=[UNKNOWN]))
    trade(ex)
    assert halt_file.read_text().startswith("2026-09-27T17:30:00 UTC order ") and "has an unknown fate" in halt_file.read_text()
    logs = []
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, cash, again = executor(tmp_path, venues, logs=logs)           # A crash or a deploy restarts the process.
    assert again.halted.startswith(f"halted before this start, remove {halt_file} to resume: ")
    assert logs[0].startswith("live trading halted before this start") and trade(again) == [False] and venues.orders == []
    halt_file.unlink()                                                  # Checked and cleared by a human.
    conn, cash, resumed = executor(tmp_path, venues)
    assert resumed.halted is None and trade(resumed) == [True]
