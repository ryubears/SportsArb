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

    def book(self, entry):
        """
        Apply a cash movement, given as a Ledger entry: a buy, a sale, a payout, or a transfer.
        """
        raise NotImplementedError

    def largest(self):
        return max(self.amounts, key=self.amounts.get)

    def smallest(self):
        return min(self.amounts, key=self.amounts.get)

    def average(self):
        return sum(self.amounts.values()) / len(self.amounts)

    def summary(self):
        """
        The balances in one phrase, for log lines.
        """
        return ", ".join(f"{venue} {amount:,.0f}$" for venue, amount in self.amounts.items())
