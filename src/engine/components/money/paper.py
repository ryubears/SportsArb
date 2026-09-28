"""
Paper cash per venue, backed by the ledger.

Every ledger entry records the balance it left behind, so a venue's
balance is simply its newest entry and survives a restart without
replaying the history. A venue's ledger opens with a 'transfer_in' of
config.PAPER_START_BALANCE, written the first time the venue has no
entries, so the ledger alone accounts for every dollar. Money for an order
in flight is reserved without a ledger entry and released when the order
comes back.
"""

from common.timeutil import now_iso
from common.venues import VENUES
from db import database
from db.models import Ledger
from engine.components.money.balances import Balances
from engine.helper import config


class PaperBalances(Balances):
    """
    The paper cash on each venue, and the ledger behind it.
    """

    mode = "paper"

    def __init__(self, conn, start=None):
        self.conn = conn
        last = database.last_balances(conn)
        self.amounts = {venue: last.get(venue, 0.0) for venue in VENUES}
        for venue in VENUES:
            if venue not in last:
                self.apply(Ledger(now_iso(), venue, config.PAPER_START_BALANCE if start is None else start, "transfer_in"))

    def floor(self):
        return config.PAPER_CASH_FLOOR

    def reserve(self, venue, dollars):
        self.amounts[venue] -= dollars

    def release(self, venue, dollars):
        self.amounts[venue] += dollars

    def apply(self, entry):
        """
        Apply a Ledger entry to its venue and store it with the balance it leaves.
        """
        self.amounts[entry.venue] += entry.amount
        entry.balance = self.amounts[entry.venue]
        database.add_ledger(self.conn, entry)
