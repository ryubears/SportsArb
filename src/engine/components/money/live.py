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

Kalshi splits its cash by exchange shard, baseball's on shard 3 and
football's on 0, and an order spends only its market's shard's. Its
balance call gives each shard's with the whole, and each shard is kept
the same way, read, moved, and reserved, so an order on a shard can
spend no more than that shard has free, whatever the venue as a whole
has. Payouts are left to the next reading there.
"""

import asyncio
from collections import defaultdict
from api import kalshi, polymarket_us
from common.log import with_traceback
from common.periodic import Periodic
from common.venues import VENUES
from engine.components.money.balances import Balances
from engine.helper import config

# How each venue reports the dollars available to trade, and each exchange shard's, as (dollars, {shard: dollars}) from one
# call. Polymarket US keeps its cash in one place, so it has no shards.
READERS = {"kalshi": kalshi.balances, "polymarket_us": lambda: (polymarket_us.balance(), {})}


class LiveBalances(Balances):
    """
    The real cash on each venue, as last read plus what our orders moved since.
    readers maps a venue to a function returning its dollars and each of
    its shards' as READERS do, by default READERS.
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
        self.shard_read = {}                        # (venue, shard) maps to what the venue said the shard held at its last reading.
        self.shard_moved = defaultdict(float)       # What our orders moved on each shard since, which the reading may not show.
        self.shard_reserved = defaultdict(float)    # Held back on each shard for orders in flight.

    @property
    def amounts(self):
        return {venue: self.read[venue] + self.moved[venue] - self.reserved[venue] for venue in VENUES}

    def known(self, venue):
        return self.read_at[venue] is not None

    def available(self, venue, shard=None):
        """
        The venue's free cash, and no more than its shard has free when the
        venue keeps shards: nothing on a shard the last reading did not list.
        """
        free = self[venue]
        if shard is None or not any(v == venue for v, _ in self.shard_read):
            return free
        key = (venue, shard)
        return min(free, self.shard_read.get(key, 0.0) + self.shard_moved[key] - self.shard_reserved[key])


    def total(self):
        """
        The cash on both venues, what is held back for orders in flight and
        payouts not yet read included, so the live money does not dip while
        a batch of trades settles before the venues are read again.
        """
        return sum(self.read[venue] + self.moved[venue] + self.paid[venue] for venue in VENUES)

    def reserve(self, venue, dollars, shard=None):
        self.reserved[venue] += dollars
        if shard is not None:
            self.shard_reserved[(venue, shard)] += dollars

    def release(self, venue, dollars, shard=None):
        self.reserved[venue] -= dollars
        if shard is not None:
            self.shard_reserved[(venue, shard)] -= dollars

    def apply(self, entry, shard=None):
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
            if shard is not None:
                self.shard_moved[(entry.venue, shard)] += entry.amount

    async def refresh(self, now):
        """
        Read every venue's balance. A venue that fails keeps its last reading
        and is logged, with the traceback only when its error is new.
        """
        before, paid, shards_before = dict(self.moved), dict(self.paid), dict(self.shard_moved)
        first = not any(self.read_at.values())
        readings = await asyncio.gather(*(asyncio.to_thread(self.readers[venue]) for venue in VENUES), return_exceptions=True)
        for venue, reading in zip(VENUES, readings):
            if isinstance(reading, BaseException):
                message = f"live balance of {venue} could not be read ({reading!r}), keeping the last"
                self.log(message if self.failing.get(venue) == repr(reading) else with_traceback(message, reading))
                self.failing[venue] = repr(reading)
                continue
            self.failing.pop(venue, None)
            dollars, shards = reading
            self.read[venue] = float(dollars)
            self.moved[venue] -= before[venue]
            self.paid[venue] -= paid[venue]
            self.read_at[venue] = now
            for key in [key for key in self.shard_read if key[0] == venue]:
                del self.shard_read[key]
            for shard, amount in shards.items():
                self.shard_read[(venue, shard)] = float(amount)
            for key in [key for key in self.shard_moved if key[0] == venue]:
                self.shard_moved[key] -= shards_before.get(key, 0.0)
        if first and any(self.read_at.values()):
            self.log(f"live balances read: {self.summary()}")

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a reading when one is due and none is running.
        """
        self.readings.tick(clock, lambda: self.refresh(now))
