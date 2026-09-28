"""
Live cash per venue, read from the venues' own accounts.

The live executor spends real money, so its balances come from each
venue's own balance call rather than a ledger of ours. Each venue's cash
is kept as four numbers:

- read: what the venue said at its last reading. Readings come every
  config.LIVE_BALANCE_SECONDS, in a background thread, and at once after
  a payout.
- moved: what our own orders moved since. The venue's number lags our
  trades, so they are added to it, and a burst of trades cannot spend
  the same dollars twice.
- paid: payouts since, which count as live money but are not spent until
  a reading shows them.
- reserved: what is held back for orders in flight.

The cash free to trade is read plus moved less reserved. A new reading
replaces only the moves made before it was asked for: an order that
filled while the balance was being read may not show in it yet, so it
stays counted until the next reading. Before the first reading every
venue holds nothing, so nothing is traded.
"""

import asyncio
from api import kalshi, polymarket_us
from common.log import with_traceback
from common.periodic import Periodic
from common.venues import VENUES
from engine.components.money.balances import Balances
from engine.helper import config

READERS = {"kalshi": kalshi.balance, "polymarket_us": polymarket_us.balance}    # How each venue reports the dollars available to trade.


class LiveBalances(Balances):
    """
    The real cash on each venue, as last read plus what our orders moved since.
    readers maps a venue to a function returning its balance in dollars.
    """

    mode = "live"

    def __init__(self, log=print, readers=None):
        self.log = log
        self.readers = readers or READERS
        self.read = {venue: 0.0 for venue in VENUES}         # What each venue said at its last reading.
        self.moved = {venue: 0.0 for venue in VENUES}        # What our orders moved since, which the reading may not show.
        self.paid = {venue: 0.0 for venue in VENUES}         # Payouts since, which count as live money but are not spent until read.
        self.reserved = {venue: 0.0 for venue in VENUES}     # Held back for orders in flight.
        self.read_at = {venue: None for venue in VENUES}     # When each venue was last read, ISO 8601 UTC.
        self.readings = Periodic(lambda: config.LIVE_BALANCE_SECONDS, log, "live balance reading")
        self.failing = {}           # Venue maps to the error its last reading failed with, while it fails.

    @property
    def amounts(self):
        return {venue: self.read[venue] + self.moved[venue] - self.reserved[venue] for venue in VENUES}

    def known(self, venue):
        return self.read_at[venue] is not None

    def floor(self):
        return config.LIVE_CASH_FLOOR

    def total(self):
        """
        The cash on both venues, what is held back for orders in flight and
        payouts not yet read included, so the live money does not dip while
        a batch of trades settles before the venues are read again.
        """
        return sum(self.read[venue] + self.moved[venue] + self.paid[venue] for venue in VENUES)

    def reserve(self, venue, dollars):
        self.reserved[venue] += dollars

    def release(self, venue, dollars):
        self.reserved[venue] -= dollars

    def apply(self, entry):
        """
        Apply what one of our orders moved, a Ledger entry that is not stored,
        since the venue keeps the record. A payout is not spent until the
        venue's next reading shows it, which is asked for at once, since the
        venue pays it on its own and it may already be in the last one.
        """
        if entry.reason == "payout":
            self.paid[entry.venue] += entry.amount
            self.readings.again()
        else:
            self.moved[entry.venue] += entry.amount

    async def refresh(self, now):
        """
        Read every venue's balance. A venue that fails keeps its last reading
        and is logged, with the traceback only when its error is new.
        """
        before, paid = dict(self.moved), dict(self.paid)
        first = not any(self.read_at.values())
        readings = await asyncio.gather(*(asyncio.to_thread(self.readers[venue]) for venue in VENUES), return_exceptions=True)
        for venue, reading in zip(VENUES, readings):
            if isinstance(reading, BaseException):
                message = f"live balance of {venue} could not be read ({reading!r}), keeping the last"
                self.log(message if self.failing.get(venue) == repr(reading) else with_traceback(message, reading))
                self.failing[venue] = repr(reading)
                continue
            self.failing.pop(venue, None)
            self.read[venue] = float(reading)
            self.moved[venue] -= before[venue]
            self.paid[venue] -= paid[venue]
            self.read_at[venue] = now
        if first and any(self.read_at.values()):
            self.log(f"live balances read: {self.summary()}")

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a reading when one is due and none is running.
        """
        self.readings.tick(clock, lambda: self.refresh(now))
