"""
What our live orders took from the venues' books, so paper can give it back.

When live trades games in play beside paper, see live.py, both take the
same signals, see paper.py. The live orders are real: each takes contracts
from the levels of its venue's book, and from the moment it reaches the
venue the books on the tapes show those levels smaller. Paper must not see
that, or it would be judged on what our own live order left it. So each
live order leaves a Footprint, and paper adds what it took back to each
level it took from, in every book the venue made from then on, until the
level falls below what our order left of it, when other takers or cancels
would have taken ours too, or KEEP_SECONDS have passed.

What a live order took from each level is worked out as the venue fills it,
by sweeping the book the venue had just before the order reached it, from a
tape, up to what the venue said filled, no further than the order's limit.
That needs a tape kept from before the order arrived, as a paper trade on
the same signal keeps, see market/tape.py. Once worked out it is kept, so
later books of the contract get it back too.
"""

import asyncio
import dataclasses
import time
from collections import defaultdict
from dataclasses import dataclass, field
from api import orders
from engine.helper.pricing import book_level, book_sizes, ladder, sell_ladder, takes

KEEP_SECONDS = 10       # How long after a live order was sent paper still gives back what it took, at most.
ANSWER_SECONDS = 2.0    # How long paper waits for the answer to a live order it may have to give back, at most.
BEFORE = 0.001          # How long before a live order reached its venue the book it swept was made, at the venues' millisecond.


@dataclass
class Footprint:
    """
    One live order on one contract, and what it took from each level of the contract's book once that is worked out.
    """
    key: tuple              # (venue, contract_id).
    polarity: str           # The side the contract pays on, as the leg's.
    side: str               # The side of the bet the leg holds, 'yes' or 'no'.
    selling: bool           # Whether the order sold, flattening a trade, rather than bought to open one.
    limit: float            # The most a buy paid, or the least a sale took, per contract.
    sent: float             # When it was sent, by our clock, in seconds since 1970.
    arrived: float | None = None    # When the venue handled it, by its own clock, or None until the answer says.
    filled: float = 0       # Contracts it bought or sold.
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    took: dict | None = None        # (book side, price) maps to contracts it took from that level, once worked out.
    left: dict | None = None        # (book side, price) maps to what it left of that level, once worked out.

    def measure(self, tape):
        """
        Work out what the order took from each level, from the book the
        venue had just before it arrived on tape. Returns whether it is
        known, which needs a tape that goes back that far.
        """
        if self.took is not None:
            return True
        if tape is None or self.arrived is None or not tape.reaches(self.arrived - BEFORE):
            return False
        book = tape.at(self.arrived - BEFORE)
        if book is None:
            return False
        levels = sell_ladder(book, self.polarity, self.side) if self.selling else ladder(book, self.polarity, self.side)
        sizes = book_sizes(book)
        self.took, self.left = {}, {}
        for price, contracts in takes(levels, self.filled, limit=self.limit, step=orders.STEP, selling=self.selling):
            level = book_level(self.polarity, self.side, price, self.selling)
            self.took[level] = self.took.get(level, 0) + contracts
            self.left[level] = max(sizes.get(level, 0) - self.took[level], 0)
        return True


class Footprints:
    """
    The live orders of the last KEEP_SECONDS on each contract, which live trading leaves and paper trading gives back.
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self.by_key = defaultdict(list)     # (venue, contract_id) maps to its Footprints, oldest first.

    def sent(self, leg, selling, limit):
        """
        A live order on a leg is being sent. Returns its Footprint, to be given the venue's answer.
        """
        now = self.clock()
        for key in list(self.by_key):
            self.by_key[key] = [f for f in self.by_key[key] if now - f.sent <= KEEP_SECONDS]
            if not self.by_key[key]:
                del self.by_key[key]
        footprint = Footprint(leg.key, leg.polarity, leg.side, selling, limit, now)
        self.by_key[leg.key].append(footprint)
        return footprint

    @staticmethod
    def answer(footprint, venue, response, filled):
        """
        The venue answered a live order: how much it filled, and when it handled the order by the venue's clock.
        """
        footprint.arrived, footprint.filled = orders.venue_time(venue, response), filled
        footprint.answered.set()

    async def known(self, key, by):
        """
        Wait, ANSWER_SECONDS at most, for the answers to the live orders on
        a contract sent before by, our clock in seconds, which may have
        reached the venue before a paper order sent then.
        """
        waiting = [asyncio.ensure_future(f.answered.wait()) for f in self.by_key.get(key, ()) if f.sent < by and not f.answered.is_set()]
        if waiting:
            _, late = await asyncio.wait(waiting, timeout=ANSWER_SECONDS)
            for task in late:
                task.cancel()

    def give_back(self, key, book, tape=None):
        """
        A book of a contract with what our live orders took from it added
        back to each level, when the venue made the book after they reached
        it and the level has not fallen below what they left. tape, the
        contract's, works out what an order took when that is not known yet.
        """
        if book is None or key not in self.by_key:
            return book
        made = book.at
        sizes = book_sizes(book)
        extra = defaultdict(float)
        for f in self.by_key[key]:
            if not f.filled or f.arrived is None or made is None or made < f.arrived or not f.measure(tape):
                continue
            for level, contracts in f.took.items():
                if sizes.get(level, 0) >= f.left[level] - 1e-9:
                    extra[level] += contracts
        if not extra:
            return book
        for level, contracts in extra.items():
            sizes[level] = orders.exact(sizes.get(level, 0) + contracts)
        sides = {side: sorted(([price, size] for (on, price), size in sizes.items() if on == side), reverse=side == "bids")
                 for side in ("bids", "asks")}
        return dataclasses.replace(book, **sides)
