"""
Trade the scanner's signals, the part paper and live trading share.

When the scanner sees a pair with enough net edge, the executor sends one
limit order per leg. Each leg's limit is the deepest level that still
leaves config.MIN_EDGE when both ladders are walked together, so the order
sweeps every level above the floor, not just the top one. How an order
reaches its venue and what comes back is the one thing that differs:
paper.py fills it against the in memory books after a simulated latency,
live.py sends it to the venue. Everything else is here.

When the two legs fill unevenly the executor goes flat at once. It either
sells the excess back on its own venue or buys the missing amount on the
other venue, whichever the books say leaves more money, and records the
result with fees. What it cannot flatten stays on a list and is tried
again on every tick, against the books as they are then, until it is
flat, the bet pays out, or the settler says its contracts have resolved.
The list is read back from the trades table when the process starts, so a
restart does not leave a trade exposed. The settler leaves alone a trade
while an order to flatten it is in flight.
Only games being played are traded, so capital turns over the same day,
and an Allocator from allocate.py caps each trade so the money covers every
game in play. Every trade is stored in the trades table as soon as it is
sent and updated when it is done, and every dollar moved goes through
the cash the executor was given. Settling what was bought is settle.py's job.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from common.log import on_failure
from common.timeutil import now_iso, seconds_between
from common.venues import VENUES
from db import database
from db.models import Ledger, Trade
from engine.components.allocate import cap_range
from engine.helper import config, game
from engine.helper.pricing import depth, ladder, reach, sell_ladder, sweep, trade_words


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
    def contract_id(self):
        return self.member["contract_id"]

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


class Executor:
    """
    Turns scanner signals into trades against the recorder's books. A
    subclass says how an order is filled, through fill() and sell_back().
    books is a function returning the newest Book of each contract, keyed by (venue, contract_id).
    cash is the money on each venue, with reserve(), release(), and apply().
    """

    mode = None     # 'paper' or 'live', set by each subclass. It starts every log line.

    def __init__(self, conn, cash, books, log=print, allocator=None, clock=now_iso):
        if cash.mode != self.mode:
            raise ValueError(f"a {self.mode} executor cannot trade {cash.mode} money")
        self.conn = conn
        self.cash = cash
        self.books = books
        self.clock = clock          # The current time in ISO 8601 UTC, for fills and for how old a book is.
        self.log = log
        self.allocator = allocator
        self.tasks = set()          # Trades in flight, and the retry of exposed ones while it runs.
        self.done = []              # Trades finished since the last summary.
        self.exposed = {}           # Trade id maps to (Trade, [yes Leg, no Leg]) for trades holding more on one side than the other.
        self.flattening = set()     # Ids of exposed trades with an order in flight to flatten them, which the settler leaves alone.
        self.set_aside = {}         # Trade id maps to why no more orders are sent for it, which only live trading does.
        self.low = set()            # Venues whose free cash is under the floor, so new trades wait.
        self.retrying = None        # The task flattening exposed trades while one runs.
        self.totals = {"trades": 0, "profit": 0.0, "hedge": 0.0}
        self.reload_exposed()

    # ORDERS, which each subclass fills its own way.

    def fresh_book(self, key):
        """
        The newest book for a contract, or None when there is none or it has
        not changed for more than config.MAX_BOOK_AGE seconds, the same rule
        the scanner prices by. A market that has closed may stop changing
        rather than empty its book, and its last book cannot be traded, so
        an order or a flatten treats a stale book as no book. A quiet market
        that is still open waits for its next change.
        """
        book = self.books().get(key)
        if book is None or seconds_between(book.ts, self.clock()) > config.MAX_BOOK_AGE:
            return None
        return book

    async def fill(self, trade, leg, purpose):
        """
        Buy leg.quantity contracts of one leg of a trade at no more than
        leg.limit each, to open the trade or to flatten it, as purpose says.
        Returns a Fill.
        """
        raise NotImplementedError

    async def sell_back(self, trade, leg, quantity, floor):
        """
        Sell back contracts held through a leg of a trade at no less than
        floor each, where the books said they would sell. Returns a Fill whose
        dollars are the proceeds after fees.
        """
        raise NotImplementedError

    def flatten_limit(self, reached):
        """
        The limit for an order that buys the missing side of an exposed trade,
        given the deepest price the books said it would pay.
        """
        raise NotImplementedError

    # TRADES

    def record_holdings(self, trade, legs):
        """
        Copy what the legs hold, and what that cost, onto the trade, and set its status from the matched count.
        """
        trade.yes_held, trade.no_held = legs[0].held, legs[1].held
        trade.yes_cost, trade.no_cost = legs[0].cost, legs[1].cost
        trade.status = "failed" if trade.matched == 0 else "partial" if trade.matched < trade.quantity else "filled"

    def sale_value(self, leg, excess, average):
        """
        What selling the excess back on its own venue would bring, from the
        leg's fresh book, as (ladder, gain): the ladder it would sell into,
        and the proceeds after fees less what those contracts cost at the
        average price. The gain is None when there is no fresh book or
        nothing would sell.
        """
        book = self.fresh_book(leg.key)
        if not book:
            return [], None
        selling = sell_ladder(book, leg.polarity, leg.side)
        sold, proceeds = sweep(selling, excess, leg.venue, leg.fee_info, config.FILL_SHARE, selling=True)
        return selling, (proceeds - sold * average if sold else None)

    def purchase_value(self, leg, excess, average):
        """
        What buying the missing side on the other venue would bring, from
        the leg's fresh book, as (ladder, affordable, gain): the ladder it
        would buy from, how many of the excess that venue's cash can pay for,
        and the gain, a dollar for each pair it completes less what the
        excess contracts cost at the average price and what the purchase
        costs with fees. The gain is None when nothing would fill.
        """
        book = self.fresh_book(leg.key)
        if not book:
            return [], 0, None
        buying = ladder(book, leg.polarity, leg.side)
        affordable = min(excess, int(self.cash[leg.venue] // buying[0][0])) if buying else 0
        bought, cost = sweep(buying, affordable, leg.venue, leg.fee_info, config.FILL_SHARE)
        return buying, affordable, (bought * (1 - average) - cost if bought else None)

    async def sell_excess(self, trade, leg, excess, average, selling):
        """
        Sell the excess back on its own venue, no lower than the book said
        it would sell, and record what came back. Returns (contracts sold, note).
        """
        fill = await self.sell_back(trade, leg, excess, reach(selling, excess, config.FILL_SHARE))
        if fill.filled:
            self.cash.apply(Ledger(fill.ts, leg.venue, fill.dollars, "sell", trade.id))
        leg.held -= fill.filled
        leg.cost -= fill.filled * average
        trade.hedge_pnl += fill.dollars - fill.filled * average
        return fill.filled, f"sold back {fill.filled} of {excess} on {leg.venue}"

    async def buy_missing(self, trade, leg, excess, average, buying, affordable):
        """
        Buy the missing side on the other venue, up to what its cash can
        pay for, and record what it cost. Its limit comes from the subclass's
        flatten_limit(). Returns (contracts bought, note).
        """
        limit = self.flatten_limit(reach(buying, affordable, config.FILL_SHARE))
        self.cash.reserve(leg.venue, affordable * limit)
        fill = await self.fill(trade, dataclasses.replace(leg, quantity=affordable, limit=limit), "flatten")
        self.cash.release(leg.venue, affordable * limit)
        if fill.filled:
            self.cash.apply(Ledger(fill.ts, leg.venue, -fill.dollars, "buy", trade.id))
        leg.held += fill.filled
        leg.cost += fill.dollars
        trade.hedge_pnl += fill.filled * (1 - average) - fill.dollars
        trade.matched += fill.filled
        return fill.filled, f"bought {fill.filled} of {excess} on {leg.venue}"

    async def flatten(self, trade, legs):
        """
        Undo the excess on the leg holding more, by selling it back on its
        venue or buying the missing amount on the other venue, whichever the
        books say leaves more money. The chosen order is sent like any other.
        Returns what was done in words, or None when no fresh book allowed
        anything or the trade has been set aside.
        """
        if trade.id in self.set_aside:
            return None
        long_leg, short_leg = sorted(legs, key=lambda leg: leg.held, reverse=True)
        excess = long_leg.held - short_leg.held
        average = long_leg.cost / long_leg.held         # What each contract the long leg holds cost.
        selling, sale_gain = self.sale_value(long_leg, excess, average)
        buying, affordable, purchase_gain = self.purchase_value(short_leg, excess, average)
        if sale_gain is None and purchase_gain is None:
            return None
        if purchase_gain is None or (sale_gain is not None and sale_gain >= purchase_gain):
            done, note = await self.sell_excess(trade, long_leg, excess, average, selling)
        else:
            done, note = await self.buy_missing(trade, short_leg, excess, average, buying, affordable)
        if done < excess:
            note += f", {excess - done} exposed"
        return note

    def record_fills(self, trade, fills):
        """
        Copy what each leg's order filled onto the trade, and the profit
        locked in on the contracts both legs hold, which pay a dollar a pair
        whichever way the game goes.
        """
        yes, no = fills
        trade.yes_filled, trade.yes_cost, trade.yes_latency_ms, trade.yes_fill_ts = yes.filled, yes.dollars, yes.ms, yes.ts
        trade.no_filled, trade.no_cost, trade.no_latency_ms, trade.no_fill_ts = no.filled, no.dollars, no.ms, no.ts
        trade.matched = min(yes.filled, no.filled)
        trade.profit = trade.matched * (1 - yes.average - no.average)

    async def run_trade(self, trade, legs):
        """
        Fill both legs, flatten any mismatch, and record the result.
        """
        fills = await asyncio.gather(*(self.fill(trade, leg, "open") for leg in legs))
        for leg, fill in zip(legs, fills):
            self.cash.release(leg.venue, leg.quantity * leg.limit)
            leg.held, leg.cost = fill.filled, fill.dollars
            if fill.filled:
                self.cash.apply(Ledger(fill.ts, leg.venue, -fill.dollars, "buy", trade.id))
        self.record_fills(trade, fills)
        trade.hedge_pnl = 0.0
        hedge = None                # How the mismatch was flattened, in words, when there was one.
        if trade.yes_filled != trade.no_filled:
            exposed = abs(trade.yes_filled - trade.no_filled)
            hedge = await self.flatten(trade, legs) or f"{exposed} exposed, {self.set_aside.get(trade.id, 'no book to flatten')}"
        self.record_holdings(trade, legs)
        notes = [f"{leg.side} leg {fill.note}" for leg, fill in zip(legs, fills) if fill.note]
        trade.hedge = ", ".join(([hedge] if hedge else []) + notes) or "none"
        database.update_trade(self.conn, trade)
        if legs[0].held != legs[1].held and trade.id not in self.set_aside:
            self.exposed[trade.id] = (trade, legs)
        self.totals["trades"] += 1
        self.totals["profit"] += trade.profit
        self.totals["hedge"] += trade.hedge_pnl
        self.done.append(trade)
        self.log(f"{self.mode} {trade.status}: {trade.label}, {trade.trade}, wanted {trade.quantity}, filled {trade.yes_filled}/{trade.no_filled}, "
                 f"locked in {trade.profit:.2f}$, hedge {trade.hedge} {trade.hedge_pnl:+.2f}$")

    def settled(self, trade_id):
        """
        Called by the settler when a trade has settled. Its contracts have resolved, so it is not flattened any more.
        """
        self.exposed.pop(trade_id, None)

    def reload_exposed(self):
        """
        Take back this mode's trades left holding more on one side than the
        other when the process last stopped, so they are flattened again. A
        trade past its payout time is left to settle as it stands, and one
        with an order of unknown outcome is left to a human.
        """
        for trade, *fee_infos in database.load_exposed_trades(self.conn, self.mode, self.clock()):
            self.exposed[trade.id] = (trade, [Leg(leg.side, {"venue": leg.venue, "contract_id": leg.contract, "polarity": leg.polarity},
                                                  fee_info, leg.limit, trade.quantity, leg.held, leg.cost)
                                              for leg, fee_info in zip(trade.legs(), fee_infos)])
        if self.exposed:
            self.log(f"{self.mode} trades left exposed before this start, flattening again: {', '.join(map(str, self.exposed))}")

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
            if trade_id in self.set_aside:
                del self.exposed[trade_id]
                trade.hedge += f", then {self.set_aside[trade_id]}"
                database.update_trade(self.conn, trade)
                continue
            before = trade.hedge_pnl
            self.flattening.add(trade_id)
            try:
                note = await self.flatten(trade, legs)
            finally:
                self.flattening.discard(trade_id)
            if not note or note.startswith(("sold back 0", "bought 0")):
                continue
            self.record_holdings(trade, legs)
            left = abs(legs[0].held - legs[1].held)
            trade.hedge += f", then {note} at {now[11:19]}"
            database.update_trade(self.conn, trade)
            self.totals["hedge"] += trade.hedge_pnl - before
            self.log(f"{self.mode} flattened {trade.label}: {note}, {left} still exposed, hedge {trade.hedge_pnl:+.2f}$")
            if not left:
                del self.exposed[trade_id]

    def summary(self):
        """
        One line for the trades finished since the last summary and the running totals.
        """
        recent = self.done
        self.done = []
        counts = {s: sum(1 for t in recent if t.status == s) for s in ("filled", "partial", "failed")}
        waiting = f"; new trades wait on {', '.join(sorted(self.low))}, under the floor" if self.low else ""
        return (f"{self.mode}: {len(recent)} trades ({counts['filled']} filled, {counts['partial']} partial, {counts['failed']} failed), "
                f"locked in {sum(t.profit for t in recent):.2f}$, hedges {sum(t.hedge_pnl for t in recent):+.2f}$; "
                f"total {self.totals['trades']} trades, {self.totals['profit'] + self.totals['hedge']:.2f}$; balances {self.cash.summary()}{waiting}")

    # SIGNALS

    def spawn(self, coroutine):
        """
        Run a coroutine as a task this executor keeps until it is done, logging it if it fails.
        """
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(on_failure(self.log, f"{self.mode} trade task"))
        return task

    def floor(self):
        """
        Dollars new trades leave untouched on each venue, so the money is
        never run down to nothing and flattening, which may use it, still can.
        """
        return config.CASH_FLOOR

    def spendable(self, venue):
        """
        Dollars a new trade may spend on a venue: its free cash less the floor.
        """
        return max(0.0, self.cash[venue] - self.floor())

    def quantity_for(self, pair, legs, now):
        """
        Set each leg's limit and return how many contracts to ask for, with
        the cap that applied, as (quantity, cap). The two ladders are walked
        together through the levels that keep config.MIN_EDGE, each limit
        set at the deepest level reached. The quantity is config.FILL_SHARE
        of what those levels show, the share we expect to get, so an
        unchanged book fills in full, and no more than the cap or what each
        venue can spend.
        """
        books = self.books()
        yes_leg, no_leg = legs
        yes_ladder = ladder(books[yes_leg.key], yes_leg.polarity, "yes")
        no_ladder = ladder(books[no_leg.key], no_leg.polarity, "no")
        yes_leg.limit, no_leg.limit, available = depth(yes_ladder, no_ladder, (yes_leg.venue, yes_leg.fee_info),
                                                       (no_leg.venue, no_leg.fee_info), config.MIN_EDGE)
        cap = self.allocator.cap(pair, now) if self.allocator else cap_range(self.mode)[1]
        if not available:
            return 0, cap
        affordable = min(self.spendable(leg.venue) // leg.limit for leg in legs)
        return int(min(available * config.FILL_SHARE, cap, affordable)), cap

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Called by the scanner when a pair shows an edge. Sends the two legs
        when the edge, the game being in play, and the balances allow. Returns
        True when orders were sent, so the scanner sends no more for this episode.
        The scanner's size counts every level with a positive edge, while the
        legs are sized from the levels that keep config.MIN_EDGE, see
        quantity_for(). The cost is reserved here, before anything is awaited,
        so a second signal in the same moment sees what is left.
        """
        if edge < config.MIN_EDGE:
            return False
        kickoff = game.kickoff((yes, no))
        if not kickoff or not game.in_play(kickoff, now, pair["sport"]):
            return False
        legs = [Leg(side, member, fee_infos[(member["venue"], member["contract_id"])]) for side, member in (("yes", yes), ("no", no))]
        quantity, cap = self.quantity_for(pair, legs, now)
        if quantity < 1:
            return False
        for leg in legs:
            leg.quantity = quantity
            self.cash.reserve(leg.venue, quantity * leg.limit)
        yes_leg, no_leg = legs
        trade = Trade(mode=self.mode, pair_id=pair["id"], label=pair["label"], trade=trade_words(yes, no), signal_ts=now, edge=edge,
                      quantity=quantity, cap=cap, pays_at=game.pays_at((yes, no), pair["sport"]),
                      yes_venue=yes["venue"], yes_contract=yes["contract_id"], yes_polarity=yes["polarity"], yes_limit=yes_leg.limit,
                      no_venue=no["venue"], no_contract=no["contract_id"], no_polarity=no["polarity"], no_limit=no_leg.limit)
        database.insert_trade(self.conn, trade)
        self.spawn(self.run_trade(trade, legs))
        return True

    def tick(self, now):
        """
        Once a second from the session. Tries again to flatten what is still
        exposed, and logs when a venue goes under the floor or back over it.
        """
        if self.exposed and (self.retrying is None or self.retrying.done()):
            self.retrying = self.spawn(self.retry(now))
        floor = self.floor()
        for venue in VENUES:
            low = self.cash.known(venue) and self.cash[venue] < floor
            if low and venue not in self.low:
                self.low.add(venue)
                self.log(f"{self.mode} {venue} has {self.cash[venue]:,.2f}$, under its {floor:,.2f}$ floor, so new trades wait until more arrives")
            elif not low and venue in self.low:
                self.low.discard(venue)
                self.log(f"{self.mode} {venue} has {self.cash[venue]:,.2f}$, back over its {floor:,.2f}$ floor")
