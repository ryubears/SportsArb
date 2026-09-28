"""
Tests for the rebalancers: paper money moved between the venues, and live money a human is asked to move.
"""

from db import database
from db.models import Settlement, Trade, Transfer
from engine.components.balance import rebalance
from engine.components.balance.paper import PaperBalances


def test_weekly_check_moves_the_excess_and_it_lands_after_four_business_days(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 3000.0, "polymarket_us": 7000.0}     # 2000 above a 5000 average, past the 20 percent drift.
    logs = []
    r = rebalance.PaperRebalancer(conn, cash, logs.append)
    r.rebalance("2026-09-21T12:00:00+00:00")                        # A Monday, no check.
    assert database.load_transfers(conn) == []
    r.rebalance("2026-09-22T12:00:00+00:00")                        # Tuesday.
    (transfer,) = database.load_transfers(conn)
    assert (transfer.from_venue, transfer.to_venue, transfer.amount, transfer.reason) == ("polymarket_us", "kalshi", 2000, "drift")
    assert transfer.expected_at == "2026-09-28T12:00:00+00:00"     # Four business days, over the weekend.
    assert cash.amounts == {"kalshi": 3000.0, "polymarket_us": 5000.0}     # In transit, on neither venue.
    assert r.summary() == "transfers: 2,000$ polymarket_us to kalshi, due 2026-09-28"
    r.rebalance("2026-09-22T13:00:00+00:00")                        # Same Tuesday, checked once a day.
    assert len(database.load_transfers(conn)) == 1
    r.receive("2026-09-28T11:00:00+00:00")
    assert cash["kalshi"] == 3000.0
    r.receive("2026-09-28T12:00:00+00:00")
    assert cash.amounts == {"kalshi": 5000.0, "polymarket_us": 5000.0}
    assert database.load_transfers(conn)[0].arrived_at == "2026-09-28T12:00:00+00:00"
    assert r.summary() is None
    assert [tuple(x) for x in conn.execute("SELECT venue, amount, reason FROM ledger ORDER BY id")][2:] == [
        ("polymarket_us", -2000.0, "transfer_out"), ("kalshi", 2000.0, "transfer_in")]              # After the two openings.


def test_a_venue_running_low_waits_for_the_weekly_check(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 400.0, "polymarket_us": 6000.0}
    r = rebalance.PaperRebalancer(conn, cash, lambda m: None)
    r.rebalance("2026-09-23T12:00:00+00:00")                        # A Wednesday.
    assert database.load_transfers(conn) == []
    r.rebalance("2026-09-29T12:00:00+00:00")                        # The next Tuesday.
    (transfer,) = database.load_transfers(conn)
    assert (transfer.reason, transfer.amount) == ("drift", 2800)


def test_a_transfer_still_in_flight_does_not_hold_up_the_weekly_check(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    database.insert_transfer(conn, Transfer("polymarket_us", "kalshi", 374.0, "2026-09-24T03:00:00+00:00",
                                            "2026-10-01T03:00:00+00:00", "floor"))        # Lands after the Tuesday check.
    cash.amounts = {"kalshi": 3900.0, "polymarket_us": 6100.0}     # 1100 above a 5000 average: 22 percent, past 20.
    r = rebalance.PaperRebalancer(conn, cash, lambda m: None)
    r.rebalance("2026-09-29T12:00:00+00:00")
    assert [(t.amount, t.reason, t.arrived_at) for t in database.load_transfers(conn)] == [(374.0, "floor", None), (1100.0, "drift", None)]
    assert cash.amounts == {"kalshi": 3900.0, "polymarket_us": 5000.0}


def test_balanced_venues_need_no_transfer(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 4600.0, "polymarket_us": 5400.0}     # 8 percent apart.
    r = rebalance.PaperRebalancer(conn, cash, lambda m: None)
    r.rebalance("2026-09-22T12:00:00+00:00")
    assert database.load_transfers(conn) == []


def open_trade(conn):
    """
    A filled trade whose contracts have not settled yet.
    """
    conn.execute("INSERT INTO pairs (label, kind, venues, contracts, flags, matched_at) VALUES ('p', 'game_winner', '', 2, '[]', 'm')")
    t = Trade(mode="paper", pair_id=1, trade="t", signal_ts="2026-09-22T00:30:00+00:00", edge=0.05, quantity=5, yes_venue="polymarket_us",
              yes_contract="pm", yes_polarity="yes", yes_limit=0.45, no_venue="kalshi", no_contract="k", no_polarity="yes",
              no_limit=0.47, pays_at="2026-09-22T04:15:00+00:00", yes_held=5, no_held=5, status="filled")
    database.insert_trade(conn, t)
    return t


def test_the_tuesday_check_waits_for_monday_nights_trades_to_settle(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.amounts = {"kalshi": 3000.0, "polymarket_us": 7000.0}
    r = rebalance.PaperRebalancer(conn, cash, lambda m: None)
    t = open_trade(conn)                                            # Monday night's game, still out after midnight UTC.
    r.rebalance("2026-09-22T01:00:00+00:00")
    assert database.load_transfers(conn) == []
    database.insert_settlement(conn, Settlement(t.id, "2026-09-22T04:20:00+00:00", mode="paper"))
    r.rebalance("2026-09-22T04:30:00+00:00")                        # Still Tuesday, and nothing is open now.
    assert [x.reason for x in database.load_transfers(conn)] == ["drift"]


# LIVE

import asyncio
import pytest
from engine.components import notify
from engine.components.balance.live import LiveBalances


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
    cash = live_money(300.0, 700.0)                     # 200 above a 500 average, past the 20 percent drift.
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


def test_live_venues_close_enough_or_with_money_out_are_not_emailed(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    emails = []
    rebalancer_for(conn, live_money(460.0, 540.0), emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")     # 8 percent apart.
    open_trade(conn)
    conn.execute("UPDATE trades SET mode = 'live'")
    rebalancer_for(conn, live_money(300.0, 700.0), emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")     # Waits for the trade.
    unread = LiveBalances(lambda m: None, {})
    conn.execute("DELETE FROM trades")
    rebalancer_for(conn, unread, emails, tmp_path, monkeypatch).check("2026-09-28T00:00:00+00:00")                      # Nothing read yet.
    assert emails == []


def test_an_alert_without_email_settings_is_still_logged_and_stored(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "missing.json")
    logs = []
    notifier = notify.Notifier(conn, logs.append, sender=lambda *args: pytest.fail("nothing to send with"))

    async def scenario():
        notifier.send("halt", "SportsArb live trading halted", "why")      # From inside the loop the email goes out in the background.
        await asyncio.gather(*notifier.tasks)
    asyncio.run(scenario())
    (stored,) = [dict(r) for r in conn.execute("SELECT kind, sent_at, error FROM alerts")]
    assert stored == {"kind": "halt", "sent_at": None, "error": "no email settings in missing.json"}
    assert logs == ["alert: SportsArb live trading halted", "alert 1 was not emailed: no email settings in missing.json"]


def test_an_email_upgrades_to_tls_logs_in_and_sends(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            calls.append(("connect", host, port, "tls" if context else "plain"))

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"], message.get_content().strip()))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    settings = {"host": "smtp.example.com", "port": 587, "user": "bot", "password": "p", "from": "bot@example.com", "to": ["a@x.com", "b@x.com"]}
    notify.send_email(settings, "subject", "body")
    assert calls == [("connect", "smtp.example.com", 587, "plain"), ("starttls",), ("login", "bot"),
                     ("send", "a@x.com, b@x.com", "subject", "body"), ("quit",)]
    calls.clear()
    notify.send_email(dict(settings, port=465, user=None), "subject", "body")
    assert calls[0] == ("connect", "smtp.example.com", 465, "tls") and ("starttls",) not in calls and calls[1][0] == "send"
