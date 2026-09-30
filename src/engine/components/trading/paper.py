"""
Paper trade the scanner's signals against the live books.

The paper executor pretends to send each order. It arrives at its venue
after a random latency drawn from what we measured, and fills against the
book as it is at that moment, from the same in memory books the scanner
reads. A level still there fills, a level that shrank fills partly, and a
level that is gone does not fill. Only a share of the visible size is
assumed to be ours, since other takers see the same thing, and a small
share of orders is rejected outright.

A real order takes the contracts it fills, but a paper one leaves the
venue's book as it was, so an edge that stays, or comes back, would be
filled again from the same contracts. So the paper executor remembers what
its orders took from each level and sees every book without it, when it
sizes, fills, and flattens. What it took from a level counts until the
level shrinks below that or goes, as other takers or cancels would have
taken ours too. It learns that when it next looks at the book, so a level
gone and back between two looks still has ours taken off, never more.

Everything else, the sizing, the flattening, and storing each trade, is
the shared Executor's, and the money is the PaperBalances from
money/paper.py.
"""

import asyncio
import dataclasses
import math
import random
from common.timeutil import now_iso
from engine.components.trading.executor import Executor, Fill
from engine.helper import config
from engine.helper.pricing import book_level, ladder, sell_ladder, sweep, takes


class PaperExecutor(Executor):
    """
    Fills orders against the recorder's books after a simulated latency.
    """

    mode = "paper"

    def __init__(self, conn, cash, books, log=print, rng=None, clock=now_iso):
        super().__init__(conn, cash, books, log, clock)
        self.rng = rng or random.Random()
        self.taken = {}             # (venue, contract_id) maps to {(book side, price): contracts our orders took from that level}.

    def book(self, key):
        """
        The newest book for a contract less what our orders took from it. A
        level smaller than what we took, or gone, has lost ours with the rest,
        so what we took is kept down to it, and forgotten once it is gone.
        """
        book = super().book(key)
        ours = self.taken.get(key)
        if book is None or not ours:
            return book
        still = {}                  # What we took from the levels still there, no more than each holds now.
        sides = {}
        for side in ("bids", "asks"):
            sides[side] = []
            for price, size in getattr(book, side):
                level = (side, round(price, 4))
                mine = min(ours.get(level, 0), size)
                if mine > 0:
                    still[level] = mine
                if size > mine:
                    sides[side].append([price, size - mine])
        if still:
            self.taken[key] = still
        else:
            del self.taken[key]
        return dataclasses.replace(book, **sides)

    def take(self, leg, levels, quantity, limit=None, selling=False):
        """
        Fill an order for quantity of a leg from levels, the ladder made from
        its book, or the sell_ladder() when selling, as pricing.sweep() does,
        and remember what it took from each level of the book. Returns
        (contracts, dollars).
        """
        ours = self.taken.setdefault(leg.key, {})
        for price, contracts in takes(levels, quantity, config.FILL_SHARE, limit):
            level = book_level(leg.polarity, leg.side, price, selling)
            ours[level] = ours.get(level, 0) + contracts
        return sweep(levels, quantity, leg.venue, leg.fee_info, config.FILL_SHARE, limit=limit, selling=selling)

    def latency(self, venue):
        median, sigma = config.PAPER_LATENCY_MS[venue]
        return int(self.rng.lognormvariate(math.log(median), sigma))

    async def arrive(self, venue):
        """
        Wait for an order to reach its venue. Returns the latency in milliseconds and the time it arrived.
        """
        ms = self.latency(venue)
        await asyncio.sleep(ms / 1000)
        return ms, self.clock()

    async def fill(self, trade, leg):
        """
        Send one leg's buy order and fill it against the book as it is when the order arrives.
        """
        ms, ts = await self.arrive(leg.venue)
        if self.rng.random() < config.PAPER_REJECT_PROBABILITY:
            return Fill(ms=ms, ts=ts, note="rejected")
        book = self.fresh_book(leg.key, self.aging(trade))
        if book is None:
            return Fill(ms=ms, ts=ts, note="no book")
        filled, dollars = self.take(leg, ladder(book, leg.polarity, leg.side), leg.quantity, leg.limit)
        return Fill(filled, dollars, ms, ts)

    async def sell_back(self, trade, leg, quantity, floor):
        """
        Sell back contracts held through a leg at whatever the book offers when the order arrives, whatever the floor.
        """
        ms, ts = await self.arrive(leg.venue)
        book = self.fresh_book(leg.key, self.aging(trade))
        if book is None:
            return Fill(ms=ms, ts=ts)
        filled, dollars = self.take(leg, sell_ladder(book, leg.polarity, leg.side), quantity, selling=True)
        return Fill(filled, dollars, ms, ts)
