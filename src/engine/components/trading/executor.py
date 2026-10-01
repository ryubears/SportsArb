"""
Trade the scanner's signals, the part paper and live trading share.

When the scanner sees a pair with enough net edge, the executor sends one
limit order per leg. Each leg's limit is the deepest level that still
leaves config.MIN_EDGE when both ladders are walked together, so the order
sweeps every level above the floor, not just the top one. How an order
reaches its venue and what comes back is the one thing that differs:
paper.py fills it against the in memory books after a simulated latency,
live.py sends it to the venue. Everything else is here.

An edge is taken only while each leg's book is current. Polymarket US
books reach us some 85 ms after the venue changes them, Kalshi's in 12, so
a price that just moved on one venue can sit next to the other's old one,
an edge that is gone by the time an order arrives. A leg on a venue in
config.CONFIRM_SECONDS must have a book newer, by the venues' own clocks,
than the other leg's last change, or wait until that change is old enough
that any reaction to it would have reached us. The scanner offers the edge
again at the next change or tick, so one that is real is taken then. Once
it is taken, both legs' orders go out at once.

When the two legs fill unevenly the executor goes flat at once by selling
the excess back on its own venue, and records the result with fees. A
sale frees the money at once, where buying the missing amount on the
other venue, though it may cost less, would tie it up until the bet pays.
What it cannot sell stays on a list and is tried again on every tick,
against the books as they are then, until it is flat, the bet pays out,
or the settler says its contracts have resolved. The list is read back
from the trades table when the process starts, so a restart does not
leave a trade exposed. The settler leaves alone a trade while an order to
flatten it is in flight.

A pair is traded before its game or on a season's future, never once its
game has kicked off, while its edge is config.MIN_EDGE or more, the bet
pays out config.MIN_PAYOUT_HOURS or more away, and the edge returns
config.MIN_ANNUAL_PCT a year or more until then. Near a game and during
it, faster traders take an edge before our Polymarket US leg lands.
A trade asks for config.FILL_SHARE of what the books show at that edge,
the share we expect to get, as far as the cash free on each venue pays
for, live as on paper. Every trade is stored in the trades table as soon
as it is sent and updated when it is done, and every dollar moved goes
through the cash the executor was given. Settling what was bought is
money/settle.py's job.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from common.log import on_failure
from common.timeutil import epoch, hours_between, now_iso
from db import database
from db.models import Ledger, Leg, Trade
from engine.helper import config, game
from api.orders import exact
from engine.helper.pricing import annual_pct, depth, fresh, ladder, reach, sell_ladder, sweep, trade_words


def shard(leg):
    """
    The exchange shard a leg's market trades on, whose cash is its own, as the catalog gave it, or None for a venue without shards.
    """
    return (leg.fee_info or {}).get("exchange_index")


@dataclass
class Fill:
    """
    What came back for one order: contracts filled, dollars paid or received
    including fees, and the latency and time of the fill.
    """
    filled: float = 0           # Contracts, to the hundredth live, whole on paper.
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
    step = 1        # The least part of a contract an order trades: whole contracts on paper, a hundredth live, see orders.STEP.

    def __init__(self, conn, cash, books, log=print, clock=now_iso):
        if cash.mode != self.mode:
            raise ValueError(f"a {self.mode} executor cannot trade {cash.mode} money")
        self.conn = conn
        self.cash = cash
        self.books = books
        self.clock = clock          # The current time in ISO 8601 UTC, for fills and for how old a book is.
        self.log = log
        self.games = {}             # Trade id maps to its pair's game date and members, for when its books go stale while it is flattened.
        self.tasks = set()          # Trades in flight, and the retry of exposed ones while it runs.
        self.done = []              # Trades finished since the last summary.
        self.exposed = {}           # Trade id maps to (Trade, [yes Leg, no Leg]) for trades holding more on one side than the other.
        self.flattening = set()     # Ids of exposed trades with an order in flight to flatten them, which the settler leaves alone.
        self.set_aside = {}         # Trade id maps to why no more orders are sent for it, which only live trading does.
        self.waiting = set()        # Pairs whose edge waited for a book to catch up since the last summary, see confirmed().
        self.retrying = None        # The task flattening exposed trades while one runs.
        self.totals = {"trades": 0, "profit": 0.0, "hedge": 0.0}
        self.reload_exposed()

    # ORDERS, which each subclass fills its own way.

    def book(self, key):
        """
        The newest book for a contract as this executor trades against it, or None when there is none.
        """
        return self.books().get(key)

    def fresh_book(self, key, aging=True):
        """
        The newest book for a contract, or None when there is none or, when its
        books age, it is too old to trade, see pricing.fresh() and game.started().
        """
        book = self.book(key)
        return book if fresh(book, self.clock(), aging) else None

    def aging(self, trade):
        """
        Whether a trade's books go stale when they stop changing, as the
        scanner judges its pair's. One taken before this start is judged as a
        game's already begun, the stricter way.
        """
        kept = self.games.get(trade.id)
        return game.started(*kept, self.clock()) if kept else True

    async def fill(self, trade, leg):
        """
        Buy leg.quantity contracts of one leg of a trade at no more than
        leg.limit each, to open the trade. Returns a Fill.
        """
        raise NotImplementedError

    async def sell_back(self, trade, leg, quantity, floor):
        """
        Sell back contracts held through a leg of a trade at no less than
        floor each, where the books said they would sell. Returns a Fill whose
        dollars are the proceeds after fees.
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

    def sale_ladder(self, leg, excess, aging=True):
        """
        The ladder selling the excess back on the leg's own venue would sell
        into, from its fresh book, or an empty one when there is no fresh
        book or nothing would sell.
        """
        book = self.fresh_book(leg.key, aging)
        if not book:
            return []
        selling = sell_ladder(book, leg.polarity, leg.side)
        sold, _ = sweep(selling, excess, leg.venue, leg.fee_info, config.FILL_SHARE, selling=True, step=self.step)
        return selling if sold else []

    async def sell_excess(self, trade, leg, excess, average, selling):
        """
        Sell the excess back on its own venue, no lower than the book said
        it would sell, and record what came back. Returns (contracts sold, note).
        """
        fill = await self.sell_back(trade, leg, excess, reach(selling, excess, config.FILL_SHARE, self.step))
        if fill.filled:
            self.cash.apply(Ledger(fill.ts, leg.venue, fill.dollars, "sell", trade.id), shard(leg))
        leg.held = exact(leg.held - fill.filled)
        leg.cost -= fill.filled * average
        trade.hedge_pnl += fill.dollars - fill.filled * average
        return fill.filled, f"sold back {fill.filled:g} of {excess:g} on {leg.venue}"

    async def flatten(self, trade, legs):
        """
        Sell the excess on the leg holding more back on its own venue, sent
        like any other order. Returns what was done in words, or None when no
        fresh book would take the sale or the trade has been set aside.
        """
        if trade.id in self.set_aside:
            return None
        long_leg, short_leg = sorted(legs, key=lambda leg: leg.held, reverse=True)
        excess = exact(long_leg.held - short_leg.held)
        average = long_leg.cost / long_leg.held         # What each contract the long leg holds cost.
        selling = self.sale_ladder(long_leg, excess, self.aging(trade))
        if not selling:
            return None
        done, note = await self.sell_excess(trade, long_leg, excess, average, selling)
        if done < excess:
            note += f", {exact(excess - done):g} exposed"
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

    def forget(self, trade_id):
        """
        Stop flattening a trade, and forget its game.
        """
        self.exposed.pop(trade_id, None)
        self.games.pop(trade_id, None)

    async def run_trade(self, trade, legs):
        """
        Fill both legs, flatten any mismatch, and record the result.
        """
        fills = await asyncio.gather(*(self.fill(trade, leg) for leg in legs))
        for leg, fill in zip(legs, fills):
            self.cash.release(leg.venue, leg.quantity * leg.limit, shard(leg))
            leg.held, leg.cost = fill.filled, fill.dollars
            if fill.filled:
                self.cash.apply(Ledger(fill.ts, leg.venue, -fill.dollars, "buy", trade.id), shard(leg))
        self.record_fills(trade, fills)
        trade.hedge_pnl = 0.0
        hedge = None                # How the mismatch was flattened, in words, when there was one.
        if trade.yes_filled != trade.no_filled:
            exposed = exact(abs(trade.yes_filled - trade.no_filled))
            hedge = await self.flatten(trade, legs) or f"{exposed:g} exposed, {self.set_aside.get(trade.id, 'no book to flatten')}"
        self.record_holdings(trade, legs)
        notes = [f"{leg.side} leg {fill.note}" for leg, fill in zip(legs, fills) if fill.note]
        trade.hedge = ", ".join(([hedge] if hedge else []) + notes) or "none"
        database.update_trade(self.conn, trade)
        if legs[0].held != legs[1].held and trade.id not in self.set_aside:
            self.exposed[trade.id] = (trade, legs)
        else:
            self.forget(trade.id)
        self.totals["trades"] += 1
        self.totals["profit"] += trade.profit
        self.totals["hedge"] += trade.hedge_pnl
        self.done.append(trade)
        self.log(f"{self.mode} {trade.status}: {trade.label}, {trade.trade}, wanted {trade.quantity:g}, filled {trade.yes_filled:g}/{trade.no_filled:g}, "
                 f"locked in {trade.profit:.2f}$, hedge {trade.hedge} {trade.hedge_pnl:+.2f}$")

    def settled(self, trade_id):
        """
        Called by the settler when a trade has settled. Its contracts have resolved, so it is not flattened any more.
        """
        self.forget(trade_id)

    def reload_exposed(self):
        """
        Take back this mode's trades left holding more on one side than the
        other when the process last stopped, so they are flattened again. A
        trade past its payout time is left to settle as it stands, and one
        with an order of unknown outcome is left to a human.
        """
        for trade, *fee_infos in database.load_exposed_trades(self.conn, self.mode, self.clock()):
            self.exposed[trade.id] = (trade, [dataclasses.replace(leg, fee_info=fee_info) for leg, fee_info in zip(trade.legs(), fee_infos)])
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
                self.forget(trade_id)
                continue
            if trade_id in self.set_aside:
                self.forget(trade_id)
                trade.hedge += f", then {self.set_aside[trade_id]}"
                database.update_trade(self.conn, trade)
                continue
            before = trade.hedge_pnl
            self.flattening.add(trade_id)
            try:
                note = await self.flatten(trade, legs)
            finally:
                self.flattening.discard(trade_id)
            if not note or note.startswith("sold back 0"):
                continue
            self.record_holdings(trade, legs)
            left = exact(abs(legs[0].held - legs[1].held))
            trade.hedge += f", then {note} at {now[11:19]}"
            database.update_trade(self.conn, trade)
            self.totals["hedge"] += trade.hedge_pnl - before
            self.log(f"{self.mode} flattened {trade.label}: {note}, {left:g} still exposed, hedge {trade.hedge_pnl:+.2f}$")
            if not left:
                self.forget(trade_id)

    def summary(self):
        """
        One line for the trades finished since the last summary and the running totals.
        """
        recent = self.done
        self.done = []
        counts = {s: sum(1 for t in recent if t.status == s) for s in ("filled", "partial", "failed")}
        waiting = ""
        if self.waiting:
            waiting = f"; {len(self.waiting)} pairs' edges waited for a book to catch up"
            self.waiting = set()
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

    @staticmethod
    def pays_enough(edge, now, pays_at):
        """
        Whether an edge is worth the capital it ties up until the bet pays at
        pays_at: config.MIN_PAYOUT_HOURS or more away, and returning
        config.MIN_ANNUAL_PCT a year or more until then.
        """
        if not pays_at or hours_between(now, pays_at) < config.MIN_PAYOUT_HOURS:
            return False
        return annual_pct(edge, game.days_until(now, pays_at)) >= config.MIN_ANNUAL_PCT

    def confirmed(self, yes, no, now):
        """
        Whether each leg's book is current enough to trade on. A leg on a
        venue in config.CONFIRM_SECONDS needs a book newer, by the venues'
        own clocks, than the other leg's last change, or else that change
        must be at least that many seconds old. A book the venue gave no time
        for counts from when it reached us.
        """
        times = []
        for member in (yes, no):
            book = self.book((member["venue"], member["contract_id"]))
            if book is None:
                return False
            times.append(book.at if book.at is not None else epoch(book.ts))
        (yes_at, no_at), clock = times, epoch(now)
        for member, own, other in ((yes, yes_at, no_at), (no, no_at, yes_at)):
            if own < other and clock - other < config.CONFIRM_SECONDS.get(member["venue"], 0):
                return False
        return True

    def quantity_for(self, legs):
        """
        Set each leg's limit and return how many contracts to ask for. The
        two ladders are walked together through the levels that keep
        config.MIN_EDGE, each limit set at the deepest level reached. The
        quantity is config.FILL_SHARE of what those levels show, the share we
        expect to get, so an unchanged book fills in full, and no more than
        the cash free pays for, both legs' at once where they share a venue's
        cash.
        """
        yes_leg, no_leg = legs
        yes_book, no_book = self.book(yes_leg.key), self.book(no_leg.key)
        if yes_book is None or no_book is None:
            return 0
        yes_ladder = ladder(yes_book, yes_leg.polarity, "yes")
        no_ladder = ladder(no_book, no_leg.polarity, "no")
        yes_leg.limit, no_leg.limit, available = depth(yes_ladder, no_ladder, (yes_leg.venue, yes_leg.fee_info),
                                                       (no_leg.venue, no_leg.fee_info), config.MIN_EDGE)
        if not available:
            return 0
        per_contract = {}           # What one contract of both legs costs from each venue's cash, its shard's where it has them.
        for leg in legs:
            per_contract[(leg.venue, shard(leg))] = per_contract.get((leg.venue, shard(leg)), 0.0) + leg.limit
        affordable = min(self.cash.spendable(venue, part) // cost for (venue, part), cost in per_contract.items())
        return int(min(available * config.FILL_SHARE, affordable))

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Called by the scanner when a pair shows an edge. Sends the two legs
        when the edge, the game not having started, the time until the bet
        pays, its return a year, and the balances allow. Returns True when
        orders were sent, so the scanner sends no more for this episode. The
        scanner's size counts every level with a positive edge, while the
        legs are sized from the levels that keep config.MIN_EDGE, see
        quantity_for(). The cost is reserved here, before anything is
        awaited, so a second signal in the same moment sees what is left.
        """
        if edge < config.MIN_EDGE:
            return False
        if game.started(pair.get("game_date"), (yes, no), now):
            return False
        pays_at = game.pays_at((yes, no), pair["sport"])
        if not self.pays_enough(edge, now, pays_at):
            return False
        if not self.confirmed(yes, no, now):
            self.waiting.add(pair["id"])
            return False
        legs = [Leg(side, m["venue"], m["contract_id"], m["polarity"], fee_info=fee_infos[(m["venue"], m["contract_id"])])
                for side, m in (("yes", yes), ("no", no))]
        quantity = self.quantity_for(legs)
        if quantity < 1:
            return False
        for leg in legs:
            leg.quantity = quantity
            self.cash.reserve(leg.venue, quantity * leg.limit, shard(leg))
        yes_leg, no_leg = legs
        trade = Trade(mode=self.mode, pair_id=pair["id"], label=pair["label"], trade=trade_words(yes, no), signal_ts=now, edge=edge,
                      quantity=quantity, pays_at=pays_at,
                      yes_venue=yes["venue"], yes_contract=yes["contract_id"], yes_polarity=yes["polarity"], yes_limit=yes_leg.limit,
                      no_venue=no["venue"], no_contract=no["contract_id"], no_polarity=no["polarity"], no_limit=no_leg.limit)
        database.insert_trade(self.conn, trade)
        self.games[trade.id] = (pair.get("game_date"), (yes, no))
        self.spawn(self.run_trade(trade, legs))
        return True

    def tick(self, now):
        """
        Once a second from the session. Tries again to flatten what is still exposed.
        """
        if self.exposed and (self.retrying is None or self.retrying.done()):
            self.retrying = self.spawn(self.retry(now))
