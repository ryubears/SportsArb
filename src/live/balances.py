"""
Paper cash per venue, backed by the ledger.

Every ledger entry records the balance it left behind, so a venue's
balance is simply its newest entry and survives a restart without
replaying the history. A venue with no entries starts at config.START_BALANCE. Money
for an order in flight is reserved without a ledger entry and released
when the order comes back, so two signals in the same moment cannot both
spend the same dollars.
"""

from common import config
from common.venues import VENUES
from db import database


class Balances:
    """
    The cash on each venue, and the ledger behind it.
    """

    def __init__(self, conn, start=None):
        self.conn = conn
        last = database.last_balances(conn)
        start = config.START_BALANCE if start is None else start
        self.amounts = {venue: last.get(venue, start) for venue in VENUES}

    def __getitem__(self, venue):
        return self.amounts[venue]

    def reserve(self, venue, dollars):
        """
        Hold dollars back for an order in flight.
        """
        self.amounts[venue] -= dollars

    def release(self, venue, dollars):
        """
        Give back a reservation, or the part of it that was not spent.
        """
        self.amounts[venue] += dollars

    def book(self, entry):
        """
        Apply a Ledger entry to its venue and store it with the balance it leaves.
        """
        self.amounts[entry.venue] += entry.amount
        entry.balance = self.amounts[entry.venue]
        database.add_ledger(self.conn, entry)

    def richest(self):
        return max(self.amounts, key=self.amounts.get)

    def poorest(self):
        return min(self.amounts, key=self.amounts.get)

    def average(self):
        return sum(self.amounts.values()) / len(self.amounts)

    def summary(self):
        """
        The balances in one phrase, for log lines.
        """
        return ", ".join(f"{venue} {amount:,.0f}$" for venue, amount in self.amounts.items())
