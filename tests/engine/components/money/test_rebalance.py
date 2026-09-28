"""
Tests for the rebalancers: paper money moved between the venues, and live money a human is asked to move.
"""

from db import database
from db.models import Settlement, Trade, Transfer
from engine.components.money import rebalance
from engine.components.money.paper import PaperBalances


def test_the_daily_check_moves_the_excess_and_it_lands_after_four_business_days(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 3000.0, "polymarket_us": 7000.0}     # 2000 above a 5000 average, past the 10 percent drift.
    logs = []
    r = rebalance.PaperRebalancer(conn, cash, logs.append)
    r.rebalance("2026-09-22T09:59:00+00:00")                        # Before the hour, no check.
    assert database.load_transfers(conn) == []
    r.rebalance("2026-09-22T10:00:00+00:00")
    (transfer,) = database.load_transfers(conn)
    assert (transfer.from_venue, transfer.to_venue, transfer.amount, transfer.reason) == ("polymarket_us", "kalshi", 2000, "drift")
    assert transfer.expected_at == "2026-09-28T10:00:00+00:00"     # Four business days, over the weekend.
    assert cash.amounts == {"kalshi": 3000.0, "polymarket_us": 5000.0}     # In transit, on neither venue.
    assert r.summary() == "transfers: 2,000$ polymarket_us to kalshi, due 2026-09-28"
    r.rebalance("2026-09-22T13:00:00+00:00")                        # Checked once a day.
    r.rebalance("2026-09-23T10:00:00+00:00")                        # The next day the 2,000 on its way counts for Kalshi.
    assert len(database.load_transfers(conn)) == 1
    r.receive("2026-09-28T09:00:00+00:00")
    assert cash["kalshi"] == 3000.0
    r.receive("2026-09-28T10:00:00+00:00")
    assert cash.amounts == {"kalshi": 5000.0, "polymarket_us": 5000.0}
    assert database.load_transfers(conn)[0].arrived_at == "2026-09-28T10:00:00+00:00"
    assert r.summary() is None
    assert [tuple(x) for x in conn.execute("SELECT venue, amount, reason FROM ledger ORDER BY id")][2:] == [
        ("polymarket_us", -2000.0, "transfer_out"), ("kalshi", 2000.0, "transfer_in")]              # After the two openings.


def test_a_venue_running_low_waits_for_the_daily_check(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 400.0, "polymarket_us": 6000.0}
    r = rebalance.PaperRebalancer(conn, cash, lambda m: None)
    r.rebalance("2026-09-23T03:00:00+00:00")                        # In the night.
    assert database.load_transfers(conn) == []
    r.rebalance("2026-09-23T10:00:00+00:00")
    (transfer,) = database.load_transfers(conn)
    assert (transfer.reason, transfer.amount) == ("drift", 2800)


def test_money_on_its_way_counts_for_the_venue_it_is_going_to(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    database.insert_transfer(conn, Transfer("polymarket_us", "kalshi", 374.0, "2026-09-24T03:00:00+00:00",
                                            "2026-10-01T03:00:00+00:00", "floor"))
    cash.amounts = {"kalshi": 3900.0, "polymarket_us": 6100.0}
    logs = []
    r = rebalance.PaperRebalancer(conn, cash, logs.append)
    r.rebalance("2026-09-29T10:00:00+00:00")
    # Kalshi at 4,274 with the 374 on its way, so Polymarket US is 913 above the 5,187 average, not 1,100 above 5,000.
    assert [(t.amount, t.reason, t.arrived_at) for t in database.load_transfers(conn)] == [(374.0, "floor", None), (913.0, "drift", None)]
    assert cash.amounts == {"kalshi": 3900.0, "polymarket_us": 5187.0}
    assert logs[0].endswith("with kalshi 4,274$ (374$ on its way), polymarket_us 6,100$")


def test_venues_more_than_10_percent_apart_are_rebalanced_and_closer_ones_are_not(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 4600.0, "polymarket_us": 5400.0}     # 8 percent above the average.
    r = rebalance.PaperRebalancer(conn, cash, lambda m: None)
    r.rebalance("2026-09-22T10:00:00+00:00")
    assert database.load_transfers(conn) == []
    cash.amounts = {"kalshi": 4400.0, "polymarket_us": 5600.0}     # 12 percent.
    r.rebalance("2026-09-23T10:00:00+00:00")
    assert [t.amount for t in database.load_transfers(conn)] == [600.0]


def open_trade(conn, yes_cost, no_cost, mode="paper"):
    """
    A filled trade whose contracts have not settled yet, holding its yes leg on Polymarket US and its no leg on Kalshi.
    """
    conn.execute("INSERT OR IGNORE INTO pairs (id, label, kind, venues, contracts, flags, matched_at) VALUES (1, 'p', 'game_winner', '', 2, '[]', 'm')")
    t = Trade(mode=mode, pair_id=1, trade="t", signal_ts="2026-09-22T00:30:00+00:00", edge=0.05, quantity=5, yes_venue="polymarket_us",
              yes_contract="pm", yes_polarity="yes", yes_limit=0.45, no_venue="kalshi", no_contract="k", no_polarity="yes",
              no_limit=0.47, pays_at="2026-09-22T04:15:00+00:00", yes_held=5, no_held=5, yes_cost=yes_cost, no_cost=no_cost, status="filled")
    database.insert_trade(conn, t)
    return t


def test_the_check_counts_what_open_trades_hold_at_cost(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 3000.0, "polymarket_us": 5000.0}     # 25 percent apart in free cash alone.
    r = rebalance.PaperRebalancer(conn, cash, lambda m: None)
    t = open_trade(conn, yes_cost=200.0, no_cost=1800.0)           # Not settled yet at the check.
    r.rebalance("2026-09-22T10:00:00+00:00")
    assert database.load_transfers(conn) == []                      # 4,800 against 5,200 counting the trade: 4 percent, under 10.
    database.insert_settlement(conn, Settlement(t.id, "2026-09-22T04:20:00+00:00", mode="paper"))
    assert rebalance.drift(conn, cash).totals == {"kalshi": 3000.0, "polymarket_us": 5000.0}      # Settled, so not held.


def test_a_transfer_moves_only_what_is_free_above_the_floor(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 2000.0, "polymarket_us": 1500.0}
    logs = []
    r = rebalance.PaperRebalancer(conn, cash, logs.append)
    open_trade(conn, yes_cost=6500.0, no_cost=0.0)                  # Polymarket US at 8,000 in all, 3,000 above a 5,000 average.
    r.rebalance("2026-09-22T12:00:00+00:00")
    (transfer,) = database.load_transfers(conn)
    assert (transfer.from_venue, transfer.to_venue, transfer.amount) == ("polymarket_us", "kalshi", 1000.0)    # 1,500 free, less the 500 floor.
    assert logs[0].endswith("with kalshi 2,000$, polymarket_us 8,000$ (6,500$ of it in open trades)")


# LIVE

import asyncio
from engine.components.money.live import LiveBalances
from engine.components.trading import notify


def live_money(kalshi, polymarket_us):
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: kalshi, "polymarket_us": lambda: polymarket_us})
    asyncio.run(cash.refresh("2026-09-27T23:00:00+00:00"))
    return cash


def rebalancer_for(conn, cash, emails, tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "email.json")
    (tmp_path / "email.json").write_text('{"host": "smtp.example.com", "from": "bot@example.com", "to": ["me@example.com"]}')
    notifier = notify.Notifier(conn, lambda m: None, sender=lambda settings, subject, body: emails.append((settings["to"], subject, body)))
    return rebalance.LiveRebalancer(conn, cash, notifier)


def test_live_venues_apart_are_emailed_once_a_day_and_nothing_is_moved(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = live_money(300.0, 700.0)                     # 200 above a 500 average, past the 10 percent drift.
    emails = []
    rebalancer = rebalancer_for(conn, cash, emails, tmp_path, monkeypatch)
    rebalancer.check("2026-09-28T00:00:00+00:00")             # A Monday, any day will do.
    ((to, subject, body),) = emails
    assert (to, subject) == (["me@example.com"], "SportsArb: move 200$ from polymarket_us to kalshi")
    assert "Move 200.00$ from polymarket_us to kalshi" in body and "kalshi 300$, polymarket_us 700$" in body
    (stored,) = [dict(r) for r in conn.execute("SELECT * FROM alerts")]
    assert (stored["kind"], stored["error"]) == ("rebalance", None) and stored["sent_at"]
    rebalancer.check("2026-09-28T12:00:00+00:00")             # Still apart, but asked for lately.
    rebalancer.check("2026-09-29T00:00:00+00:00")             # A day on, asked again.
    assert len(emails) == 2
    assert database.load_transfers(conn) == [] and cash.amounts == {"kalshi": 300.0, "polymarket_us": 700.0}


def test_live_venues_12_percent_apart_are_past_the_10_percent_drift(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    emails = []
    rebalancer_for(conn, live_money(440.0, 560.0), emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")     # 60 above 500.
    ((_, subject, body),) = emails
    assert subject == "SportsArb: move 60$ from polymarket_us to kalshi" and "past the 10% that calls for a transfer" in body


def test_live_venues_close_enough_or_not_yet_read_are_not_emailed(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    emails = []
    rebalancer_for(conn, live_money(460.0, 540.0), emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")     # 8 percent apart.
    open_trade(conn, yes_cost=0.0, no_cost=300.0, mode="live")
    rebalancer_for(conn, live_money(300.0, 700.0), emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")     # 600 against 700.
    unread = LiveBalances(lambda m: None, {})
    rebalancer_for(conn, unread, emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")                      # Nothing read yet.
    assert emails == []


def test_live_venues_apart_with_trades_open_are_asked_to_move_only_the_free_cash(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    emails = []
    open_trade(conn, yes_cost=550.0, no_cost=0.0, mode="live")     # Polymarket US at 700 in all, 200 above a 500 average.
    rebalancer_for(conn, live_money(300.0, 150.0), emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")
    ((_, subject, body),) = emails
    assert subject == "SportsArb: move 145$ from polymarket_us to kalshi"      # 150 free, less the 5 floor.
    assert "kalshi 300$, polymarket_us 700$ (550$ of it in open trades)" in body and "Move 145.00$ from polymarket_us to kalshi" in body
    assert "The rest of its 200.00$ excess is held in open trades and cannot move until they settle." in body
