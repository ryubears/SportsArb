"""
Keep both venues funded by moving paper money between them.

Balances drift apart as games resolve, because the venue holding the
winning leg receives the whole dollar and the other receives nothing. On
config.REBALANCE_WEEKDAY the balances are compared, and when the richer venue
sits more than config.REBALANCE_DRIFT above the two venue average the excess is sent to
the other one. A venue under config.REBALANCE_FLOOR is topped up on any day. A transfer
takes config.TRANSFER_DAYS business days, during which the money is on neither
venue, and one is in flight at a time. Every transfer is stored.
"""

from datetime import datetime
from common import config
from common.timeutil import add_business_days
from db import database
from db.models import Ledger, Transfer


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
        Request a transfer from the richer venue to the poorer one when they
        have drifted apart on the weekly check, or at any time when a venue
        is under the floor.
        """
        if database.load_transfers(self.conn, pending_only=True):
            return
        rich, poor = self.cash.richest(), self.cash.poorest()
        excess = self.cash[rich] - self.cash.average()
        reason = None
        today = now[:10]
        if datetime.fromisoformat(now).weekday() == config.REBALANCE_WEEKDAY and self.last_check != today:
            self.last_check = today
            if excess > config.REBALANCE_DRIFT * self.cash.average():
                reason = "drift"
        if reason is None and self.cash[poor] < config.REBALANCE_FLOOR and excess > 0:
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
