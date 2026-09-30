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
from engine.helper.pricing import ladder, sell_ladder, sweep, takes


class PaperExecutor(Executor):
    """
    Fills orders against the recorder's books after a simulated latency.
    """

    mode = "paper"

    def __init__(self, conn, cash, books, log=print, rng=None, scoreboard=None, clock=now_iso):
        super().__init__(conn, cash, books, log, scoreboard, clock)
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
        sides = {}
        for side in ("bids", "asks"):
            kept = []
            for price, size in getattr(book, side):
                level = (side, round(price, 4))
                took = min(ours.pop(level, 0), size)
                if took > 0:
                    ours[level] = took
                if size - took > 0:
                    kept.append([price, size - took])
            sides[side] = kept
        for level in [level for level in ours if not any(round(p, 4) == level[1] for p, _ in getattr(book, level[0]))]:
            del ours[level]
        if not ours:
            del self.taken[key]
        return dataclasses.replace(book, **sides)

    def took(self, key, side, levels, quantity, limit=None):
        """
        Remember what an order for quantity took from one side of a book,
        'asks' or 'bids', walking the ladder of costs made from it as the
        order did. A cost from the bids is one less the bid.
        """
        ours = self.taken.setdefault(key, {})
        for cost, contracts in takes(levels, quantity, config.FILL_SHARE, limit):
            level = (side, round(cost if side == "asks" else 1 - cost, 4))
            ours[level] = ours.get(level, 0) + contracts

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

    async def fill(self, trade, leg, purpose):
        """
        Send one leg's buy order and fill it against the book as it is when the order arrives.
        """
        ms, ts = await self.arrive(leg.venue)
        if self.rng.random() < config.PAPER_REJECT_PROBABILITY:
            return Fill(ms=ms, ts=ts, note="rejected")
        book = self.fresh_book(leg.key, self.aging(trade))
        if book is None:
            return Fill(ms=ms, ts=ts, note="no book")
        levels = ladder(book, leg.polarity, leg.side)
        filled, dollars = sweep(levels, leg.quantity, leg.venue, leg.fee_info, config.FILL_SHARE, limit=leg.limit)
        self.took(leg.key, "asks" if leg.side == leg.polarity else "bids", levels, leg.quantity, leg.limit)
        return Fill(filled, dollars, ms, ts)

    async def sell_back(self, trade, leg, quantity, floor):
        """
        Sell back contracts held through a leg at whatever the book offers when the order arrives, whatever the floor.
        """
        ms, ts = await self.arrive(leg.venue)
        book = self.fresh_book(leg.key, self.aging(trade))
        if book is None:
            return Fill(ms=ms, ts=ts)
        levels = sell_ladder(book, leg.polarity, leg.side)
        filled, dollars = sweep(levels, quantity, leg.venue, leg.fee_info, config.FILL_SHARE, selling=True)
        self.took(leg.key, "bids" if leg.side == leg.polarity else "asks", levels, quantity)
        return Fill(filled, dollars, ms, ts)

    def flatten_limit(self, reached):
        """
        No limit: a paper order to flatten takes what the book has when it arrives.
        """
        return 1.0
