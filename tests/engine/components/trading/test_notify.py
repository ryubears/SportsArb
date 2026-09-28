"""
Tests for the email alerts.
"""

import asyncio
import pytest
from db import database
from engine.components.trading import notify


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
