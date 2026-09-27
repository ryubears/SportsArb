"""
Paper trade the scanner's signals against the live books.

The paper executor pretends to send each order. It arrives at its venue
after a random latency drawn from what we measured, and fills against the
book as it is at that moment, from the same in memory books the scanner
reads. A level still there fills, a level that shrank fills partly, and a
level that is gone does not fill. Only a share of the visible size is
assumed to be ours, since other takers see the same thing, and a small
share of orders is rejected outright. Everything else, the sizing, the
flattening, and storing each trade, is the shared Executor's, and the money
is the paper Balances from balances.py.
"""

import asyncio
import math
import random
from common.timeutil import now_iso
from live.components.execute.executor import Executor, Fill
from live.helper import config
from live.helper.pricing import ladder, sell_ladder, sweep


class PaperExecutor(Executor):
    """
    Fills orders against the recorder's books after a simulated latency.
    """

    mode = "paper"

    def __init__(self, conn, cash, books, log=print, rng=None, allocator=None, clock=now_iso):
        super().__init__(conn, cash, books, log, allocator, clock)
        self.rng = rng or random.Random()

    def latency(self, venue):
        median, sigma = config.LATENCY_MS[venue]
        return int(self.rng.lognormvariate(math.log(median), sigma))

    async def arrive(self, venue):
        """
        Wait for an order to reach its venue. Returns the latency in milliseconds and the time it arrived.
        """
        ms = self.latency(venue)
        await asyncio.sleep(ms / 1000)
        return ms, self.clock()

    async def fill(self, trade, leg, purpose):
        """
        Send one leg's buy order and fill it against the book as it is when the order arrives.
        """
        ms, ts = await self.arrive(leg.venue)
        if self.rng.random() < config.REJECT_PROBABILITY:
            return Fill(ms=ms, ts=ts, note="rejected")
        quote = self.book(leg.key)
        if quote is None:
            return Fill(ms=ms, ts=ts, note="no book")
        filled, dollars = sweep(ladder(quote, leg.polarity, leg.side), leg.quantity, leg.venue, leg.fee_info, config.FILL_SHARE, limit=leg.limit)
        return Fill(filled, dollars, ms, ts)

    async def sell_back(self, trade, leg, quantity, floor):
        """
        Sell back contracts held through a leg at whatever the book offers when the order arrives, whatever the floor.
        """
        ms, ts = await self.arrive(leg.venue)
        quote = self.book(leg.key)
        if quote is None:
            return Fill(ms=ms, ts=ts)
        filled, dollars = sweep(sell_ladder(quote, leg.polarity, leg.side), quantity, leg.venue, leg.fee_info, config.FILL_SHARE, selling=True)
        return Fill(filled, dollars, ms, ts)

    def flatten_limit(self, reached):
        """
        No limit: a paper order to flatten takes what the book has when it arrives.
        """
        return 1.0
