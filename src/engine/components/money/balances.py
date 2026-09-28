"""
The money on each venue, the shape paper and live trading share.

paper.py keeps paper money in a ledger of our own, and live.py reads real
money from the venues. Either way the executor, allocator, settler, and
rebalancing see the same thing: the dollars free on each venue, less what
is held back for orders in flight, so two signals in the same moment
cannot spend the same dollars.
"""


class Balances:
    """
    The free cash on each venue, as amounts, which each subclass keeps its own way.
    """

    mode = None     # 'paper' or 'live', set by each subclass: the trades this money pays for, so the settler, allocator, and rebalancing read only those.

    def __getitem__(self, venue):
        return self.amounts[venue]

    def known(self, venue):
        """
        Whether the venue's cash is known yet.
        """
        return True

    def floor(self):
        """
        Dollars new trades leave untouched on each venue, so the money is
        never run down to nothing and flattening, which may use it, still can.
        """
        raise NotImplementedError

    def spendable(self, venue):
        """
        Dollars new trades may spend on the venue: its free cash above the floor, or nothing while it is not known.
        """
        return max(0.0, self[venue] - self.floor()) if self.known(venue) else 0.0

    def reserve(self, venue, dollars):
        """
        Hold dollars back for an order in flight.
        """
        raise NotImplementedError

    def release(self, venue, dollars):
        """
        Give back a reservation, or the part of it that was not spent.
        """
        raise NotImplementedError

    def apply(self, entry):
        """
        Apply a cash movement, given as a Ledger entry: a buy, a sale, a payout, or a transfer.
        """
        raise NotImplementedError

    def summary(self):
        """
        The balances in one phrase, for log lines.
        """
        return ", ".join(f"{venue} {amount:,.0f}$" for venue, amount in self.amounts.items())
