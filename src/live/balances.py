"""
Paper cash per venue, backed by the ledger.

Each venue's balance is the starting amount plus every ledger entry for
it, so it survives a restart. Money for an order in flight is reserved
without a ledger entry and released when the order comes back, so two
signals in the same moment cannot both spend the same dollars.
"""

from common.venues import VENUES
from db import database

BALANCE = 5000.0    # Paper dollars per venue at the start.


class Balances:
    """
    The cash on each venue, and the ledger behind it.
    """

    def __init__(self, conn, start=BALANCE):
        self.conn = conn
        totals = database.ledger_totals(conn)
        self.amounts = {venue: start + totals.get(venue, 0.0) for venue in VENUES}

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
        Apply a Ledger entry to its venue and store it.
        """
        self.amounts[entry.venue] += entry.amount
        database.add_ledger(self.conn, entry)

    def richest(self):
        return max(self.amounts, key=self.amounts.get)

    def poorest(self):
        return min(self.amounts, key=self.amounts.get)

    def average(self):
        return sum(self.amounts.values()) / len(self.amounts)

    def words(self):
        """
        The balances in one phrase, for log lines.
        """
        return ", ".join(f"{venue} {amount:,.0f}$" for venue, amount in self.amounts.items())
