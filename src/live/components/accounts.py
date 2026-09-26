"""
Live cash per venue, read from the venues' own accounts.

The live executor spends real money, so its balances come from each
venue's balance call rather than a ledger of our own. They are read every
config.LIVE_BALANCE_SECONDS in a background thread, and sooner after a
payout. Between readings the money our own orders move is applied to the
last reading, so a burst of trades does not spend the same dollars twice.
A reading only replaces the movements made before it was asked for, since
a later one may not show in it yet: an order filled while the balance was
being read is counted in full until the next reading. Money for an order
in flight is reserved in memory, as with the paper Balances, whose shape
Accounts share so the executor, allocator, and settler take either.
Before the first reading every venue holds nothing, so nothing is traded.
"""

import asyncio
from api import kalshi, polymarket_us
from common.log import on_failure, with_traceback
from common.venues import VENUES
from live.helper import config

READERS = {"kalshi": kalshi.balance, "polymarket_us": polymarket_us.balance}    # How each venue reports the dollars available to trade.


class Accounts:
    """
    The real cash on each venue, as last read plus what our orders moved since.
    readers maps a venue to a function returning its balance in dollars.
    """

    mode = "live"       # The trades this money pays for, so the settler, allocator, and alert read only those.

    def __init__(self, log=print, readers=None):
        self.log = log
        self.readers = readers or READERS
        self.read = {venue: 0.0 for venue in VENUES}         # What each venue said at its last reading.
        self.moved = {venue: 0.0 for venue in VENUES}        # What our orders moved since, which the reading may not show.
        self.reserved = {venue: 0.0 for venue in VENUES}     # Held back for orders in flight.
        self.read_at = {venue: None for venue in VENUES}     # When each venue was last read, ISO 8601 UTC.
        self.running = None         # The reading while one runs.
        self.last_check = None      # Wall clock seconds the last reading started, None before the first.

    @property
    def amounts(self):
        return {venue: self.read[venue] + self.moved[venue] - self.reserved[venue] for venue in VENUES}

    def __getitem__(self, venue):
        return self.amounts[venue]

    def reserve(self, venue, dollars):
        """
        Hold dollars back for an order in flight.
        """
        self.reserved[venue] += dollars

    def release(self, venue, dollars):
        """
        Give back a reservation, or the part of it that was not spent.
        """
        self.reserved[venue] -= dollars

    def book(self, entry):
        """
        Apply what one of our orders moved, a Ledger entry that is not stored,
        since the venue keeps the books. A payout is left to the venue's next
        reading, which is asked for at once, since the venue pays it on its own.
        """
        if entry.reason == "payout":
            self.last_check = None
        else:
            self.moved[entry.venue] += entry.amount

    async def refresh(self, now):
        """
        Read every venue's balance. A venue that fails keeps its last reading and is logged.
        """
        before = dict(self.moved)
        first = not any(self.read_at.values())
        readings = await asyncio.gather(*(asyncio.to_thread(self.readers[venue]) for venue in VENUES), return_exceptions=True)
        for venue, reading in zip(VENUES, readings):
            if isinstance(reading, BaseException):
                self.log(with_traceback(f"live balance of {venue} could not be read ({reading!r}), keeping the last", reading))
                continue
            self.read[venue] = float(reading)
            self.moved[venue] -= before[venue]
            self.read_at[venue] = now
        if first and any(self.read_at.values()):
            self.log(f"live balances read: {self.summary()}")

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a reading when one is due and none is running.
        """
        due = self.last_check is None or clock - self.last_check >= config.LIVE_BALANCE_SECONDS
        if due and (self.running is None or self.running.done()):
            self.last_check = clock
            self.running = asyncio.create_task(self.refresh(now))
            self.running.add_done_callback(on_failure(self.log, "live balance reading"))

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
