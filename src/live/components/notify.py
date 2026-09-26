"""
Tell a human by email.

Live trading asks for a person in two cases: the live venues have drifted
apart and money should be moved between them by hand, and live trading
has halted. Every alert is logged and stored in the alerts table as it is
raised, and emailed in a background thread, so the loop never waits on the
mail server. Whether the email went out, or why not, is written back to
the alert.

The mail server and addresses are in data/email.json, gitignored like the
venue keys and read at each alert, so they can be added without a restart:

    {"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com",
     "password": "an app password", "from": "me@gmail.com", "to": ["me@gmail.com"]}

Port 465 connects over TLS from the start, any other port upgrades with
STARTTLS. Without the file the alert is still logged and stored.
"""

import asyncio
import json
import smtplib
import ssl
from email.message import EmailMessage
from common.log import on_failure
from common.paths import DATA_DIR
from common.timeutil import now_iso
from db import database
from db.models import Alert

EMAIL_FILE = DATA_DIR / "email.json"


def send_email(settings, subject, body):
    """
    Send one plain text email through the SMTP server the settings name.
    """
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, settings["from"], ", ".join(settings["to"])
    message.set_content(body)
    port = int(settings.get("port", 587))
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(settings["host"], port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(settings["host"], port, timeout=30)
        server.starttls(context=context)
    with server:
        if settings.get("user"):
            server.login(settings["user"], settings["password"])
        server.send_message(message)


class Notifier:
    """
    Stores, logs, and emails alerts. sender is the function that sends one email, given the settings, subject, and body.
    """

    def __init__(self, conn, log=print, sender=send_email):
        self.conn = conn
        self.log = log
        self.sender = sender
        self.tasks = set()          # Emails being sent.

    def send(self, kind, subject, body, now=None):
        """
        Raise an alert at now, the current time unless given. Inside the
        running loop the email goes out in a background thread, outside it at once.
        """
        alert = Alert(ts=now or now_iso(), kind=kind, subject=subject, body=body)
        database.insert_alert(self.conn, alert)
        self.log(f"alert: {subject}")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.finish(alert, self.deliver(subject, body))
            return alert
        task = asyncio.create_task(asyncio.to_thread(self.deliver, subject, body))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception() or self.finish(alert, t.result()))
        task.add_done_callback(on_failure(self.log, "alert email"))
        return alert

    def deliver(self, subject, body):
        """
        Email one alert. Returns None when it went out, or why it did not.
        """
        try:
            settings = json.loads(EMAIL_FILE.read_text())
        except FileNotFoundError:
            return f"no email settings in {EMAIL_FILE.name}"
        except (OSError, ValueError) as e:
            return f"email settings in {EMAIL_FILE.name} could not be read ({e!r})"
        try:
            self.sender(settings, subject, body)
        except Exception as e:
            return repr(e)
        return None

    def finish(self, alert, error):
        """
        Write back whether the alert's email went out, and log when it did not.
        """
        alert.sent_at, alert.error = (None, error) if error else (now_iso(), None)
        database.update_alert(self.conn, alert)
        if error:
            self.log(f"alert {alert.id} was not emailed: {error}")
