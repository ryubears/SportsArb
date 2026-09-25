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
against the books as they are then, until it is flat or the bet pays out.
Only pairs that pay out within RESOLVE_HOURS are traded, so capital turns
over daily. Every trade is stored in the trades table as soon as it is
sent and updated when it is done, and every dollar moved goes through
the shared Balances. Settling what was bought is settle.py's job, and
keeping the venues funded is rebalance.py's.
"""

import asyncio
import math
import random
from common.timeutil import now_iso, seconds_between
from db import database
from db.models import Ledger, Trade
from live import fees
from live.pricing import depth, ladder, trade_words
from live.scan import resolution_time

MIN_EDGE = 0.05             # Net dollars per contract at the top before orders are sent, and the floor for the deeper levels they sweep.
                            # In-game, 2 to 3 cent edges lost money after hedging.
MAX_QUANTITY = 50           # Contracts per trade. Both legs together cost about a dollar a contract, so this caps a trade near 50 dollars,
                            # which keeps a full slate of Sunday games within the two 10,000 dollar balances.
RESOLVE_HOURS = 24          # Only pairs paying out within this many hours are traded.
FILL_SHARE = 0.5            # The share of visible size at a level assumed to be ours. Other takers get the rest.
REJECT_PROBABILITY = 0.03   # The share of orders a venue rejects outright, for rate limits and errors.
# Signal to fill latency per venue, as median milliseconds and the sigma of a lognormal draw. From us-east-1 a signed
# request round trip is about 35 ms to Kalshi and 30 ms to Polymarket US, and on top of that sit the feed's own lag
# in showing us the book and the venue's matching, so the medians are set above the round trips.
LATENCY_MS = {"kalshi": (50, 0.35), "polymarket_us": (60, 0.35)}


class Fill:
    """
    What came back for one order: contracts filled, dollars paid or received
    including fees, and the latency and time of the fill.
    """

    def __init__(self, filled=0, dollars=0.0, ms=0, ts=None, note=""):
        self.filled = filled
        self.dollars = dollars
        self.ms = ms
        self.ts = ts
        self.note = note

    @property
    def average(self):
        return self.dollars / self.filled if self.filled else 0.0


def sell_ladder(quote, polarity, side):
    """
    Proceeds per contract and size for selling back one side of the bet held
    through this contract, best first. Holding the side the contract pays
    on means selling at the bids. Holding the other side means selling the
    opposite outcome, which fetches one minus the ask.
    """
    if side == polarity:
        return [(price, size) for price, size in quote.bids]
    return [(round(1 - price, 4), size) for price, size in quote.asks]


class PaperExecutor:
    """
    Turns scanner signals into paper trades against the recorder's books.
    books is a function returning the newest quotes keyed by (venue, contract_id).
    """

    def __init__(self, conn, cash, books, log=print, rng=None):
        self.conn = conn
        self.cash = cash
        self.books = books
        self.log = log
        self.rng = rng or random.Random()
        self.tasks = set()          # Trades in flight, and the retry of exposed ones while it runs.
        self.done = []              # Trades finished since the last summary.
        self.exposed = []           # (Trade, legs) still holding more on one side than the other.
        self.retrying = None        # The task flattening exposed trades while one runs.
        self.totals = {"trades": 0, "profit": 0.0, "hedge": 0.0}

    # SIGNALS

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Called by the scanner when a pair shows an edge. Sends the two legs
        when the edge, the payout time, and the balances allow. Returns True
        when orders were sent, so the scanner sends no more for this episode.
        The scanner's size counts every level with a positive edge. The legs
        are sized here from the levels that keep MIN_EDGE instead, and each
        limit is set at the deepest of them. The cost is reserved here,
        before anything is awaited, so a second signal in the same moment
        sees what is left.
        """
        if edge < MIN_EDGE:
            return False
        pays_at = max((t for t in (resolution_time(m["start_time"], m["close_time"]) for m in (yes, no)) if t), default=None)
        if not pays_at or seconds_between(now, pays_at) > RESOLVE_HOURS * 3600:
            return False
        books = self.books()
        legs = []
        for side, member in (("yes", yes), ("no", no)):
            key = (member["venue"], member["contract_id"])
            legs.append({"side": side, "member": member, "key": key, "fee_info": fee_infos[key],
                         "levels": ladder(books[key], member["polarity"], side)})
        legs[0]["limit"], legs[1]["limit"], available = depth(legs[0]["levels"], legs[1]["levels"],
                                                              (yes["venue"], legs[0]["fee_info"]), (no["venue"], legs[1]["fee_info"]), MIN_EDGE)
        # Ask for the share of the visible size we expect to get, so an unchanged book fills in full.
        quantity = int(min(available * FILL_SHARE, MAX_QUANTITY, *(self.cash[l["member"]["venue"]] // l["limit"] for l in legs))) if available else 0
        if quantity < 1:
            return False
        for l in legs:
            l["quantity"] = quantity
            self.cash.reserve(l["member"]["venue"], quantity * l["limit"])
        trade = Trade(pair_id=pair["id"], label=pair["label"], trade=trade_words(yes, no), signal_ts=now, edge=edge, quantity=quantity,
                      yes_venue=yes["venue"], yes_contract=yes["contract_id"], yes_polarity=yes["polarity"], yes_limit=legs[0]["limit"],
                      no_venue=no["venue"], no_contract=no["contract_id"], no_polarity=no["polarity"], no_limit=legs[1]["limit"], pays_at=pays_at)
        database.insert_trade(self.conn, trade)
        self.spawn(self.run_trade(trade, legs))
        return True

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
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

    async def fill(self, leg):
        """
        Send one leg and fill it against the book as it is when the order arrives.
        """
        venue = leg["member"]["venue"]
        ms = self.latency(venue)
        await asyncio.sleep(ms / 1000)
        ts = now_iso()
        if self.rng.random() < REJECT_PROBABILITY:
            return Fill(ms=ms, ts=ts, note="rejected")
        quote = self.books().get(leg["key"])
        if quote is None:
            return Fill(ms=ms, ts=ts, note="no book")
        fill = Fill(ms=ms, ts=ts)
        remaining = leg["quantity"]
        for price, size in ladder(quote, leg["member"]["polarity"], leg["side"]):
            if price > leg["limit"] + 1e-9:
                break
            take = int(min(remaining, size * FILL_SHARE))
            if take < 1:
                continue        # A level too small for a whole contract. The next may not be.
            fill.filled += take
            fill.dollars += take * (price + fees.fee(venue, price, 1, leg["fee_info"]))
            remaining -= take
            if remaining < 1:
                break
        return fill

    async def sell_back(self, leg, quantity):
        """
        Sell back contracts held through a leg at whatever the book offers when the order arrives.
        Returns a Fill whose dollars are the proceeds after fees.
        """
        venue = leg["member"]["venue"]
        ms = self.latency(venue)
        await asyncio.sleep(ms / 1000)
        fill = Fill(ms=ms, ts=now_iso())
        quote = self.books().get(leg["key"])
        if quote is None:
            return fill
        remaining = quantity
        for price, size in sell_ladder(quote, leg["member"]["polarity"], leg["side"]):
            take = int(min(remaining, size * FILL_SHARE))
            if take < 1:
                continue
            fill.filled += take
            fill.dollars += take * (price - fees.fee(venue, price, 1, leg["fee_info"]))
            remaining -= take
            if remaining < 1:
                break
        return fill

    def expected(self, levels, quantity, venue, fee_info, selling):
        """
        What a market order for quantity would get right now, as (contracts, dollars), to choose a hedge.
        """
        filled, dollars, remaining = 0, 0.0, quantity
        for price, size in levels:
            take = int(min(remaining, size * FILL_SHARE))
            if take < 1:
                continue
            fee = fees.fee(venue, price, 1, fee_info)
            filled += take
            dollars += take * (price - fee if selling else price + fee)
            remaining -= take
            if remaining < 1:
                break
        return filled, dollars

    # TRADES

    async def run_trade(self, trade, legs):
        """
        Fill both legs, flatten any mismatch, book the result.
        """
        fills = await asyncio.gather(*(self.fill(leg) for leg in legs))
        for leg, fill in zip(legs, fills):
            venue = leg["member"]["venue"]
            self.cash.release(venue, leg["quantity"] * leg["limit"])
            leg["held"], leg["cost"] = fill.filled, fill.dollars
            if fill.filled:
                self.cash.book(Ledger(fill.ts, venue, -fill.dollars, "buy", trade.id))
        yes_fill, no_fill = fills
        trade.yes_filled, trade.yes_cost, trade.yes_latency_ms, trade.yes_fill_ts = yes_fill.filled, yes_fill.dollars, yes_fill.ms, yes_fill.ts
        trade.no_filled, trade.no_cost, trade.no_latency_ms, trade.no_fill_ts = no_fill.filled, no_fill.dollars, no_fill.ms, no_fill.ts
        trade.matched = min(yes_fill.filled, no_fill.filled)
        trade.profit = trade.matched * (1 - yes_fill.average - no_fill.average)
        trade.hedge, trade.hedge_pnl = "none", 0.0
        if yes_fill.filled != no_fill.filled:
            trade.hedge = await self.flatten(trade, legs) or f"{abs(yes_fill.filled - no_fill.filled)} exposed, no book to flatten"
        self.settle_legs(trade, legs)
        notes = [f"{leg['side']} leg {fill.note}" for leg, fill in zip(legs, fills) if fill.note]
        if notes:
            trade.hedge = trade.hedge + ", " + ", ".join(notes) if trade.hedge != "none" else ", ".join(notes)
        database.update_trade(self.conn, trade)
        if legs[0]["held"] != legs[1]["held"]:
            self.exposed.append((trade, legs))
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
        trade.yes_held, trade.no_held = legs[0]["held"], legs[1]["held"]
        trade.yes_cost, trade.no_cost = legs[0]["cost"], legs[1]["cost"]
        trade.status = "failed" if trade.matched == 0 else "partial" if trade.matched < trade.quantity else "filled"

    async def retry(self, now):
        """
        Try once more to flatten every exposed trade against the current books.
        A trade past its payout time is left to settle as it stands.
        """
        for trade, legs in list(self.exposed):
            if now >= trade.pays_at:
                self.exposed.remove((trade, legs))
                continue
            before = trade.hedge_pnl
            note = await self.flatten(trade, legs)
            if not note or note.startswith(("sold back 0", "bought 0")):
                continue
            self.settle_legs(trade, legs)
            left = abs(legs[0]["held"] - legs[1]["held"])
            trade.hedge += f", then {note} at {now[11:19]}"
            database.update_trade(self.conn, trade)
            self.totals["hedge"] += trade.hedge_pnl - before
            self.log(f"paper flattened {trade.label}: {note}, {left} still exposed, hedge {trade.hedge_pnl:+.2f}$")
            if not left:
                self.exposed.remove((trade, legs))

    async def flatten(self, trade, legs):
        """
        Undo the excess on the leg holding more, by selling it back on its
        venue or buying the missing amount on the other venue, whichever the
        books say leaves more money. The chosen order is sent like any other.
        Returns what was done in words, or None when no book allowed anything.
        """
        long_leg, short_leg = sorted(legs, key=lambda l: l["held"], reverse=True)
        excess = long_leg["held"] - short_leg["held"]
        average = long_leg["cost"] / long_leg["held"]
        books = self.books()
        sell_value = buy_value = None
        buyable = 0
        if books.get(long_leg["key"]):
            quote = books[long_leg["key"]]
            n, dollars = self.expected(sell_ladder(quote, long_leg["member"]["polarity"], long_leg["side"]), excess,
                                       long_leg["member"]["venue"], long_leg["fee_info"], selling=True)
            sell_value = dollars - n * average if n else None
        if books.get(short_leg["key"]):
            quote = books[short_leg["key"]]
            levels = ladder(quote, short_leg["member"]["polarity"], short_leg["side"])
            # Only what the other venue's balance can pay for.
            buyable = min(excess, int(self.cash[short_leg["member"]["venue"]] // levels[0][0])) if levels else 0
            n, dollars = self.expected(levels, buyable, short_leg["member"]["venue"], short_leg["fee_info"], selling=False)
            buy_value = n * (1 - average) - dollars if n else None
        if sell_value is None and buy_value is None:
            return None
        if buy_value is None or (sell_value is not None and sell_value >= buy_value):
            fill = await self.sell_back(long_leg, excess)
            venue = long_leg["member"]["venue"]
            if fill.filled:
                self.cash.book(Ledger(fill.ts, venue, fill.dollars, "sell", trade.id))
            long_leg["held"] -= fill.filled
            long_leg["cost"] -= fill.filled * average
            trade.hedge_pnl += fill.dollars - fill.filled * average
            note = f"sold back {fill.filled} of {excess} on {venue}"
        else:
            order = dict(short_leg, quantity=buyable, limit=1.0)
            venue = short_leg["member"]["venue"]
            self.cash.reserve(venue, buyable * 1.0)
            fill = await self.fill(order)
            self.cash.release(venue, buyable * 1.0)
            if fill.filled:
                self.cash.book(Ledger(fill.ts, venue, -fill.dollars, "buy", trade.id))
            short_leg["held"] += fill.filled
            short_leg["cost"] += fill.dollars
            trade.hedge_pnl += fill.filled * (1 - average) - fill.dollars
            note = f"bought {fill.filled} of {excess} on {venue}"
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
                f"total {self.totals['trades']} trades, {self.totals['profit'] + self.totals['hedge']:.2f}$; balances {self.cash.words()}")
