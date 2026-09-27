"""
Keep both venues funded: move paper money between them, and ask a human to move live money.

Balances drift apart as games resolve, because the venue holding the
winning leg receives the whole dollar and the other receives nothing.

The paper Rebalancer moves the money itself. On
config.REBALANCE_WEEKDAY, a Tuesday so that Monday night's game has paid
out, the balances are compared, and when the larger sits more than
config.REBALANCE_DRIFT above the two venue average the excess is sent to
the other venue. A venue under config.REBALANCE_FLOOR is topped up on any
day. Either way a transfer waits until no trade is open, since money still
out in trades comes back as they settle and the balances only mean
something once it has. A transfer takes config.TRANSFER_DAYS business
days, during which the money is on neither venue, and one is in flight at
a time. Every transfer is stored.

Live money is moved by hand, so the live RebalanceAlert only emails. Once
no live trade is open, it compares the venues' balances each minute, and
when the larger sits more than config.REBALANCE_DRIFT above the average it
sends an alert saying how much to move where, again every
config.LIVE_ALERT_HOURS while they stay apart. Any day will do, since a
person decides when to move the money, and the drift rule alone covers a
venue running dry, since the live balances may be too small for a floor.
"""

from datetime import datetime
from common.timeutil import add_business_days, seconds_between
from db import database
from db.models import Ledger, Transfer
from live.helper import config


class Rebalancer:
    """
    Requests transfers when the balances call for one and lands them when their time comes.
    """

    def __init__(self, conn, cash, log=print):
        self.conn = conn
        self.cash = cash
        self.log = log
        self.last_check = None      # The date of the last weekly balance check.

    def rebalance(self, now):
        """
        Request a transfer from the larger balance to the smaller one when
        they have drifted apart on the weekly check, or on any day when a
        venue is under the floor, once no trade is open.
        """
        if database.load_transfers(self.conn, pending_only=True):
            return
        rich, poor = self.cash.largest(), self.cash.smallest()
        excess = self.cash[rich] - self.cash.average()
        today = now[:10]
        weekly = datetime.fromisoformat(now).weekday() == config.REBALANCE_WEEKDAY and self.last_check != today
        low = self.cash[poor] < config.REBALANCE_FLOOR and excess > 0
        if not (weekly or low) or database.has_open_trades(self.conn, self.cash.mode):
            return          # Nothing is due, or money is still out in trades. The weekly check waits for them too.
        reason = None
        if weekly:
            self.last_check = today
            if excess > config.REBALANCE_DRIFT * self.cash.average():
                reason = "drift"
        if reason is None and low:
            reason = "floor"
        if reason is None:
            return
        transfer = Transfer(rich, poor, round(excess, 2), now, add_business_days(now, config.TRANSFER_DAYS), reason)
        database.insert_transfer(self.conn, transfer)
        self.cash.book(Ledger(now, rich, -transfer.amount, "transfer_out"))
        self.log(f"transfer {transfer.id}: {transfer.amount:.2f}$ from {rich} to {poor} for {reason}, expected {transfer.expected_at[:16]}")

    def receive(self, now):
        """
        Credit transfers whose expected arrival has passed.
        """
        for t in database.load_transfers(self.conn, pending_only=True):
            if t.expected_at <= now:
                database.complete_transfer(self.conn, t.id, now)
                self.cash.book(Ledger(now, t.to_venue, t.amount, "transfer_in"))
                self.log(f"transfer {t.id}: {t.amount:.2f}$ arrived at {t.to_venue}")

    def tick(self, now):
        """
        Once a second from the recorder loop.
        """
        self.receive(now)
        self.rebalance(now)

    def summary(self):
        """
        One line about money in transit, or None when there is none.
        """
        pending = database.load_transfers(self.conn, pending_only=True)
        if not pending:
            return None
        return "transfers: " + ", ".join(f"{t.amount:,.0f}$ {t.from_venue} to {t.to_venue}, due {t.expected_at[:10]}" for t in pending)


class RebalanceAlert:
    """
    Emails a human through the notifier when the live venues have drifted apart. Live money is never moved here.
    """

    CHECK_SECONDS = 60      # Between comparisons of the balances.

    def __init__(self, conn, cash, notifier, log=print):
        self.conn = conn
        self.cash = cash
        self.notifier = notifier
        self.log = log
        self.last_check = None      # When the balances were last compared, ISO 8601 UTC.

    def check(self, now):
        """
        Send an alert when the balances are apart, every venue has been read, no live trade is open, and none was sent lately.
        """
        if not all(self.cash.read_at.values()) or database.has_open_trades(self.conn, self.cash.mode):
            return None
        rich, poor = self.cash.largest(), self.cash.smallest()
        average = self.cash.average()
        excess = self.cash[rich] - average
        if excess <= config.REBALANCE_DRIFT * average:
            return None
        last = database.last_alert_ts(self.conn, "rebalance")
        if last and seconds_between(last, now) < config.LIVE_ALERT_HOURS * 3600:
            return None
        body = (f"The live balances have drifted apart: {self.cash.summary()}, an average of {average:,.2f}$.\n\n"
                f"Move {excess:,.2f}$ from {rich} to {poor} to level them. {rich} is {100 * excess / average:.0f}% above the average, "
                f"past the {100 * config.REBALANCE_DRIFT:.0f}% that calls for a transfer.\n\n"
                f"No live trade is open. Until the money arrives, {poor} limits how much each live trade can hold.")
        return self.notifier.send("rebalance", f"SportsArb: move {excess:,.0f}$ from {rich} to {poor}", body, now)

    def tick(self, now):
        """
        Once a second from the session. Compares the balances once a minute.
        """
        if self.last_check is None or seconds_between(self.last_check, now) >= self.CHECK_SECONDS:
            self.last_check = now
            self.check(now)
