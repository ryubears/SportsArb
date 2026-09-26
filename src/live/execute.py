"""
Paper trade the scanner's signals against the live books.

When the scanner sees a pair with enough net edge, the executor pretends to
send one limit order per leg. Each leg's limit is the deepest level that
still leaves MIN_EDGE when both ladders are walked together, so the order
sweeps every level above the floor, not just the top one. Each order
arrives at its venue after a random latency drawn from what we measured,
and fills against the book as it is at that moment, from the same in
memory books the scanner reads. A level still there fills, a level that
shrank fills partly, and a level that is gone does not fill. Only a share
of the visible size is assumed to be ours, since other takers see the same
thing, and a small share of orders is rejected outright.

When the two legs fill unevenly the executor goes flat at once. It either
sells the excess back on its own venue or buys the missing amount on the
other venue, whichever leaves more money, and books the result with fees.
What it cannot flatten stays on a list and is tried again on every tick,
against the books as they are then, until it is flat, the bet pays out, or
the settler says its contracts have resolved. The settler leaves alone a
trade while an order to flatten it is in flight.
Only games being played are traded, so capital turns over the same day,
and an Allocator from allocate.py caps each trade so the money covers every
game in play. Every trade is stored in the trades table as soon as it is
sent and updated when it is done, and every dollar moved goes through
the shared Balances. Settling what was bought is settle.py's job, and
keeping the venues funded is rebalance.py's.
"""

import asyncio
import dataclasses
import math
import random
from dataclasses import dataclass
from common.log import on_failure
from common.timeutil import now_iso
from db import database
from db.models import Ledger, Trade
from live import allocate, gametime
from live.pricing import depth, ladder, sell_ladder, sweep, trade_words

MIN_EDGE = 0.05             # Net dollars per contract at the top before orders are sent, and the floor for the deeper levels they sweep.
                            # In-game, 2 to 3 cent edges lost money after hedging.
FILL_SHARE = 0.5            # The share of visible size at a level assumed to be ours. Other takers get the rest.
REJECT_PROBABILITY = 0.03   # The share of orders a venue rejects outright, for rate limits and errors.
# Signal to fill latency per venue, as median milliseconds and the sigma of a lognormal draw. From us-east-1 a signed
# request round trip is about 35 ms to Kalshi and 30 ms to Polymarket US, and on top of that sit the feed's own lag
# in showing us the book and the venue's matching, so the medians are set above the round trips.
LATENCY_MS = {"kalshi": (50, 0.35), "polymarket_us": (60, 0.35)}


@dataclass
class Leg:
    """
    One side of a trade: the pair member it is held through, the order sent
    for it, and what it holds after any flattening.
    """
    side: str               # 'yes' or 'no', the side of the bet this leg holds.
    member: dict            # The pair member, with its venue, contract_id, and polarity.
    fee_info: dict          # The contract's fee schedule.
    limit: float = 0.0      # The highest cost per contract the order accepts.
    quantity: int = 0       # Contracts the order asks for.
    held: int = 0           # Contracts held after the fill and any flattening.
    cost: float = 0.0       # Dollars paid for what is held, including fees.

    @property
    def venue(self):
        return self.member["venue"]

    @property
    def polarity(self):
        return self.member["polarity"]

    @property
    def key(self):
        return (self.member["venue"], self.member["contract_id"])


@dataclass
class Fill:
    """
    What came back for one order: contracts filled, dollars paid or received
    including fees, and the latency and time of the fill.
    """
    filled: int = 0
    dollars: float = 0.0
    ms: int = 0
    ts: str | None = None
    note: str = ""

    @property
    def average(self):
        return self.dollars / self.filled if self.filled else 0.0


class PaperExecutor:
    """
    Turns scanner signals into paper trades against the recorder's books.
    books is a function returning the newest quotes keyed by (venue, contract_id).
    """

    def __init__(self, conn, cash, books, log=print, rng=None, allocator=None):
        self.conn = conn
        self.cash = cash
        self.books = books
        self.log = log
        self.allocator = allocator
        self.rng = rng or random.Random()
        self.tasks = set()          # Trades in flight, and the retry of exposed ones while it runs.
        self.done = []              # Trades finished since the last summary.
        self.exposed = {}           # Trade id maps to (Trade, [yes Leg, no Leg]) for trades holding more on one side than the other.
        self.flattening = set()     # Ids of exposed trades with an order in flight to flatten them, which the settler leaves alone.
        self.retrying = None        # The task flattening exposed trades while one runs.
        self.totals = {"trades": 0, "profit": 0.0, "hedge": 0.0}

    # SIGNALS

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Called by the scanner when a pair shows an edge. Sends the two legs
        when the edge, the game being in play, and the balances allow. Returns
        True when orders were sent, so the scanner sends no more for this episode.
        The scanner's size counts every level with a positive edge. The legs
        are sized here from the levels that keep MIN_EDGE instead, and each
        limit is set at the deepest of them. The cost is reserved here,
        before anything is awaited, so a second signal in the same moment
        sees what is left.
        """
        if edge < MIN_EDGE:
            return False
        kickoff = gametime.kickoff((yes, no))
        if not kickoff or not gametime.in_play(kickoff, now):
            return False
        pays_at = gametime.pays_at((yes, no))
        books = self.books()
        legs = [Leg(side, member, fee_infos[(member["venue"], member["contract_id"])]) for side, member in (("yes", yes), ("no", no))]
        yes_leg, no_leg = legs
        yes_leg.limit, no_leg.limit, available = depth(*(ladder(books[l.key], l.polarity, l.side) for l in legs),
                                                       (yes_leg.venue, yes_leg.fee_info), (no_leg.venue, no_leg.fee_info), MIN_EDGE)
        # Ask for the share of the visible size we expect to get, so an unchanged book fills in full.
        cap = self.allocator.cap(pair, now) if self.allocator else allocate.MAX_CAP
        quantity = int(min(available * FILL_SHARE, cap, *(self.cash[l.venue] // l.limit for l in legs))) if available else 0
        if quantity < 1:
            return False
        for l in legs:
            l.quantity = quantity
            self.cash.reserve(l.venue, quantity * l.limit)
        trade = Trade(pair_id=pair["id"], label=pair["label"], trade=trade_words(yes, no), signal_ts=now, edge=edge, quantity=quantity, cap=cap,
                      yes_venue=yes["venue"], yes_contract=yes["contract_id"], yes_polarity=yes["polarity"], yes_limit=yes_leg.limit,
                      no_venue=no["venue"], no_contract=no["contract_id"], no_polarity=no["polarity"], no_limit=no_leg.limit, pays_at=pays_at)
        database.insert_trade(self.conn, trade)
        self.spawn(self.run_trade(trade, legs))
        return True

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(on_failure(self.log, "paper trade task"))
        return task

    def tick(self, now):
        """
        Once a second from the session. Tries again to flatten what is still exposed.
        """
        if self.exposed and (self.retrying is None or self.retrying.done()):
            self.retrying = self.spawn(self.retry(now))

    # ORDERS

    def latency(self, venue):
        median, sigma = LATENCY_MS[venue]
        return int(self.rng.lognormvariate(math.log(median), sigma))

    async def arrive(self, venue):
        """
        Wait for an order to reach its venue. Returns the latency in milliseconds and the time it arrived.
        """
        ms = self.latency(venue)
        await asyncio.sleep(ms / 1000)
        return ms, now_iso()

    async def fill(self, leg):
        """
        Send one leg's buy order and fill it against the book as it is when the order arrives.
        """
        ms, ts = await self.arrive(leg.venue)
        if self.rng.random() < REJECT_PROBABILITY:
            return Fill(ms=ms, ts=ts, note="rejected")
        quote = self.books().get(leg.key)
        if quote is None:
            return Fill(ms=ms, ts=ts, note="no book")
        filled, dollars = sweep(ladder(quote, leg.polarity, leg.side), leg.quantity, leg.venue, leg.fee_info, FILL_SHARE, limit=leg.limit)
        return Fill(filled, dollars, ms, ts)

    async def sell_back(self, leg, quantity):
        """
        Sell back contracts held through a leg at whatever the book offers when the order arrives.
        Returns a Fill whose dollars are the proceeds after fees.
        """
        ms, ts = await self.arrive(leg.venue)
        quote = self.books().get(leg.key)
        if quote is None:
            return Fill(ms=ms, ts=ts)
        filled, dollars = sweep(sell_ladder(quote, leg.polarity, leg.side), quantity, leg.venue, leg.fee_info, FILL_SHARE, selling=True)
        return Fill(filled, dollars, ms, ts)

    # TRADES

    async def run_trade(self, trade, legs):
        """
        Fill both legs, flatten any mismatch, book the result.
        """
        fills = await asyncio.gather(*(self.fill(leg) for leg in legs))
        for leg, fill in zip(legs, fills):
            self.cash.release(leg.venue, leg.quantity * leg.limit)
            leg.held, leg.cost = fill.filled, fill.dollars
            if fill.filled:
                self.cash.book(Ledger(fill.ts, leg.venue, -fill.dollars, "buy", trade.id))
        yes_fill, no_fill = fills
        trade.yes_filled, trade.yes_cost, trade.yes_latency_ms, trade.yes_fill_ts = yes_fill.filled, yes_fill.dollars, yes_fill.ms, yes_fill.ts
        trade.no_filled, trade.no_cost, trade.no_latency_ms, trade.no_fill_ts = no_fill.filled, no_fill.dollars, no_fill.ms, no_fill.ts
        trade.matched = min(yes_fill.filled, no_fill.filled)
        trade.profit = trade.matched * (1 - yes_fill.average - no_fill.average)
        trade.hedge, trade.hedge_pnl = "none", 0.0
        if yes_fill.filled != no_fill.filled:
            trade.hedge = await self.flatten(trade, legs) or f"{abs(yes_fill.filled - no_fill.filled)} exposed, no book to flatten"
        self.settle_legs(trade, legs)
        notes = [f"{leg.side} leg {fill.note}" for leg, fill in zip(legs, fills) if fill.note]
        if notes:
            trade.hedge = trade.hedge + ", " + ", ".join(notes) if trade.hedge != "none" else ", ".join(notes)
        database.update_trade(self.conn, trade)
        if legs[0].held != legs[1].held:
            self.exposed[trade.id] = (trade, legs)
        self.totals["trades"] += 1
        self.totals["profit"] += trade.profit
        self.totals["hedge"] += trade.hedge_pnl
        self.done.append(trade)
        self.log(f"paper {trade.status}: {trade.label}, {trade.trade}, wanted {trade.quantity}, filled {trade.yes_filled}/{trade.no_filled}, "
                 f"locked in {trade.profit:.2f}$, hedge {trade.hedge} {trade.hedge_pnl:+.2f}$")

    def settle_legs(self, trade, legs):
        """
        Copy what the legs hold onto the trade and set its status from the matched count.
        """
        trade.yes_held, trade.no_held = legs[0].held, legs[1].held
        trade.yes_cost, trade.no_cost = legs[0].cost, legs[1].cost
        trade.status = "failed" if trade.matched == 0 else "partial" if trade.matched < trade.quantity else "filled"

    def settled(self, trade_id):
        """
        Called by the settler when a trade has settled. Its contracts have resolved, so it is not flattened any more.
        """
        self.exposed.pop(trade_id, None)

    async def retry(self, now):
        """
        Try once more to flatten every exposed trade against the current books.
        A trade past its payout time is left to settle as it stands.
        """
        for trade_id, (trade, legs) in list(self.exposed.items()):
            if trade_id not in self.exposed:
                continue        # Settled while an earlier trade was being flattened.
            if now >= trade.pays_at:
                del self.exposed[trade_id]
                continue
            before = trade.hedge_pnl
            self.flattening.add(trade_id)
            try:
                note = await self.flatten(trade, legs)
            finally:
                self.flattening.discard(trade_id)
            if not note or note.startswith(("sold back 0", "bought 0")):
                continue
            self.settle_legs(trade, legs)
            left = abs(legs[0].held - legs[1].held)
            trade.hedge += f", then {note} at {now[11:19]}"
            database.update_trade(self.conn, trade)
            self.totals["hedge"] += trade.hedge_pnl - before
            self.log(f"paper flattened {trade.label}: {note}, {left} still exposed, hedge {trade.hedge_pnl:+.2f}$")
            if not left:
                del self.exposed[trade_id]

    async def flatten(self, trade, legs):
        """
        Undo the excess on the leg holding more, by selling it back on its
        venue or buying the missing amount on the other venue, whichever the
        books say leaves more money. The chosen order is sent like any other.
        Returns what was done in words, or None when no book allowed anything.
        """
        long_leg, short_leg = sorted(legs, key=lambda l: l.held, reverse=True)
        excess = long_leg.held - short_leg.held
        average = long_leg.cost / long_leg.held
        books = self.books()
        sell_value = buy_value = None
        buyable = 0
        if books.get(long_leg.key):
            n, dollars = sweep(sell_ladder(books[long_leg.key], long_leg.polarity, long_leg.side), excess,
                               long_leg.venue, long_leg.fee_info, FILL_SHARE, selling=True)
            sell_value = dollars - n * average if n else None
        if books.get(short_leg.key):
            levels = ladder(books[short_leg.key], short_leg.polarity, short_leg.side)
            # Only what the other venue's balance can pay for.
            buyable = min(excess, int(self.cash[short_leg.venue] // levels[0][0])) if levels else 0
            n, dollars = sweep(levels, buyable, short_leg.venue, short_leg.fee_info, FILL_SHARE)
            buy_value = n * (1 - average) - dollars if n else None
        if sell_value is None and buy_value is None:
            return None
        if buy_value is None or (sell_value is not None and sell_value >= buy_value):
            fill = await self.sell_back(long_leg, excess)
            if fill.filled:
                self.cash.book(Ledger(fill.ts, long_leg.venue, fill.dollars, "sell", trade.id))
            long_leg.held -= fill.filled
            long_leg.cost -= fill.filled * average
            trade.hedge_pnl += fill.dollars - fill.filled * average
            note = f"sold back {fill.filled} of {excess} on {long_leg.venue}"
        else:
            self.cash.reserve(short_leg.venue, buyable * 1.0)
            fill = await self.fill(dataclasses.replace(short_leg, quantity=buyable, limit=1.0))
            self.cash.release(short_leg.venue, buyable * 1.0)
            if fill.filled:
                self.cash.book(Ledger(fill.ts, short_leg.venue, -fill.dollars, "buy", trade.id))
            short_leg.held += fill.filled
            short_leg.cost += fill.dollars
            trade.hedge_pnl += fill.filled * (1 - average) - fill.dollars
            note = f"bought {fill.filled} of {excess} on {short_leg.venue}"
            trade.matched += fill.filled
        if fill.filled < excess:
            note += f", {excess - fill.filled} exposed"
        return note

    def summary(self):
        """
        One line for the trades finished since the last summary and the running totals.
        """
        recent = self.done
        self.done = []
        counts = {s: sum(1 for t in recent if t.status == s) for s in ("filled", "partial", "failed")}
        return (f"paper: {len(recent)} trades ({counts['filled']} filled, {counts['partial']} partial, {counts['failed']} failed), "
                f"locked in {sum(t.profit for t in recent):.2f}$, hedges {sum(t.hedge_pnl for t in recent):+.2f}$; "
                f"total {self.totals['trades']} trades, {self.totals['profit'] + self.totals['hedge']:.2f}$; balances {self.cash.summary()}")
