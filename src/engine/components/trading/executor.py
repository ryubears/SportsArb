"""
Trade the scanner's signals, the part paper and live trading share.

When the scanner sees a pair with enough net edge, the executor sends one
limit order per leg. Each leg's limit is the deepest level that still
leaves config.MIN_EDGE when both ladders are walked together, so the order
sweeps every level above the floor, not just the top one. How an order
reaches its venue and what comes back is the one thing that differs:
paper.py fills it against the book the venue had when a live order would
have reached it, live.py sends it to the venue. Everything else is here.

An edge is taken only while each leg's book is current. Polymarket US
books reach us some 85 ms after the venue changes them, Kalshi's in 12, so
a price that just moved on one venue can sit next to the other's old one,
an edge that is gone by the time an order arrives. A leg on a venue in
config.CONFIRM_SECONDS must have a book newer, by the venues' own clocks,
than the other leg's last change, or wait until that change is old enough
that any reaction to it would have reached us. The scanner offers the edge
again at the next change, or as soon as the wait ends, through recheck,
so one that is real is taken then. Once it is taken, both legs' orders go
out at once, on a future or a game not yet started. On a game under way
Polymarket US's goes first, and Kalshi's only once it has answered, for no
more than it filled, see fill_legs(). In play faster traders often take
the Polymarket US quote before our order lands, and a leg that misses
first leaves nothing to sell back. In the in-play test of 2026-10-04 live
trades sent that way made 3.84$ on 69 trades, and those sending both at
once lost 3.84$ on 78: they matched about as many contracts, but 52 of
them were left on one leg to sell back, against 12.

When the two legs fill unevenly the executor goes flat at once by selling
the excess back on its own venue, and records the result with fees. A
sale frees the money at once, where buying the missing amount on the
other venue, though it may cost less, would tie it up until the bet pays.
A sale goes no lower than config.MIN_SALE_SHARE of what the contracts
cost: below that it would give away most of what was paid, and the
contracts are kept, as a bet that may still pay out.
What it cannot sell stays on a list and is tried again on every tick,
against the books as they are then, until it is flat, the bet pays out,
or the settler says its contracts have resolved. A sale that filled
nothing is not tried again for config.SALE_RETRY_SECONDS, since a book
can show a bid an order never reaches. A sale its venue turned away for
lack of cash waits until the cash there has grown, since a sale can need
cash: each venue keeps one position per market, so selling the No one
trade holds where others hold more Yes is buying Yes. The list is read back
from the trades table when the process starts, so a restart does not
leave a trade exposed. The settler leaves alone a trade while an order to
flatten it is in flight.

Live trades a season's future, or a game before it starts, while its edge
is config.MIN_EDGE or more, the bet pays out config.MIN_PAYOUT_HOURS or
more away, and the edge returns config.MIN_ANNUAL_PCT a year or more
until then, and with run.py --live-in-play a game under way too, paying
within config.MAX_PAYOUT_HOURS, see live.py. Live takes an edge only once
it has stayed at config.MIN_EDGE or more for
config.LIVE_MIN_EDGE_SECONDS, as the scanner's episode times it, and the
scanner offers it again then, through recheck, while paper takes it when
first seen. Paper trades games before and while they are played, paying
within config.MAX_PAYOUT_HOURS, see paper.py. Both legs are always on two
venues, see pricing.best_trade(). Both venues must be trading, outside
the weekly maintenance each publishes, see common/venues.py: while one
has stopped, its feed may still show prices no order can trade at. A
trade asks for config.FILL_SHARE of what the books show at that edge, the
share we expect to get, as far as the cash free on each venue pays for,
live as on paper. Every trade is stored in the trades table as soon as it
is sent and updated when it is done, and every dollar moved goes through
the cash the executor was given. Settling what was bought is
money/settle.py's job.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from api.orders import exact
from common.log import on_failure
from common.timeutil import epoch, hours_between, now_iso
from common.venues import is_maintenance
from db import database
from db.models import Ledger, Leg, Trade
from engine.helper import config, game
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
    unfunded: bool = False      # The venue turned the order away for lack of cash, so nothing traded.

    @property
    def average(self):
        return self.dollars / self.filled if self.filled else 0.0


class Executor:
    """
    Turns scanner signals into trades against the recorder's books. A
    subclass says how an order is filled, through fill() and sell_back().
    books is a function returning the newest Book of each contract, keyed by (venue, contract_id).
    cash is the money on each venue, with reserve(), release(), and apply().
    is_maintenance says whether a venue is in its weekly maintenance at a time, (venue, ISO 8601 UTC), see common/venues.py.
    """

    mode = None     # 'paper' or 'live', set by each subclass. It starts every log line.
    in_play = False     # Whether a game is traded once it has started: paper's, which trades games.
    step = 1        # The least part of a contract an order trades: whole contracts on paper, a hundredth live, see orders.STEP.

    def __init__(self, conn, cash, books, log=print, clock=now_iso, is_maintenance=is_maintenance):
        if cash.mode != self.mode:
            raise ValueError(f"a {self.mode} executor cannot trade {cash.mode} money")
        self.conn = conn
        self.cash = cash
        self.books = books
        self.clock = clock          # The current time in ISO 8601 UTC, for fills and for how old a book is.
        self.log = log
        self.is_maintenance = is_maintenance    # Whether a venue is in its weekly maintenance at a time, see common/venues.py.
        self.games = {}             # Trade id maps to its pair's game date and members, for when its books go stale while it is flattened.
        self.tasks = set()          # Trades in flight, and the retry of exposed ones while it runs.
        self.done = []              # Trades finished since the last summary.
        self.exposed = {}           # Trade id maps to (Trade, [yes Leg, no Leg]) for trades holding more on one side than the other.
        self.flattening = set()     # Ids of exposed trades with an order in flight to flatten them, which the settler leaves alone.
        self.unfunded = {}          # Trade id maps to (venue, shard, cash) when a sale to flatten it was turned away for lack of cash: it is
                                    # tried again only once that cash has grown, by a payout, a deposit, or a sale.
        self.unsold = {}            # Trade id maps to when, in seconds since 1970, it may be flattened again after a sale that filled nothing.
        self.set_aside = {}         # Trade id maps to why no more orders are sent for it, which only live trading does.
        self.leads = {}             # Trade id maps to the venue whose leg goes first, for a trade whose legs are not sent at once.
        self.waiting = set()        # Pairs whose edge waited for a book to catch up since the last summary, see confirm_wait().
        self.held = set()           # Pairs whose edge was held until it had lasted since the last summary, see hold().
        self.recheck = None         # Called with (pair id, seconds) when an edge waits, to price the pair again once the wait ends.
        self.edge_since = None      # Called with a pair id, returns when its edge at config.MIN_EDGE or more began, see Scanner.edge_since().
        self.retrying = None        # The task flattening exposed trades while one runs.
        self.totals = {"trades": 0, "profit": 0.0, "hedge": 0.0}
        self.reload_exposed()

    # ORDERS, which each subclass fills its own way.

    def book(self, key, when=None):
        """
        The newest book for a contract as this executor trades against it, or
        None when there is none. when, a time by our clock, asks for the one
        that had reached us by then, which only paper trading keeps, see
        paper.py: live trading acts on its books as they come.
        """
        return self.books().get(key)

    def fresh_book(self, key, aging=True, when=None):
        """
        The newest book for a contract, by when when given, or None when there is
        none or, when its books age, it is too old to trade, see pricing.fresh()
        and game.started().
        """
        book = self.book(key, when)
        return book if fresh(book, when or self.clock(), aging) else None

    def aging(self, trade):
        """
        Whether a trade's books go stale when they stop changing, as the
        scanner judges its pair's. One taken before this start is judged as a
        game's already begun, the stricter way.
        """
        kept = self.games.get(trade.id)
        return game.started(*kept, self.clock()) if kept else True

    async def fill(self, trade, leg, when=None):
        """
        Buy leg.quantity contracts of one leg of a trade at no more than
        leg.limit each, to open the trade, sent at when, by our clock, or
        now. Returns a Fill.
        """
        raise NotImplementedError

    async def sell_back(self, trade, leg, quantity, floor, when=None):
        """
        Sell back contracts held through a leg of a trade at no less than
        floor each, where the books said they would sell, sent at when, by our
        clock, or now. Returns a Fill whose dollars are the proceeds after fees.
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

    def sale_ladder(self, leg, excess, aging=True, least=0.0, when=None):
        """
        The ladder selling the excess back on the leg's own venue would sell
        into, from its fresh book, by when when given, its levels at least
        least each, or an empty one when there is no fresh book or nothing
        would sell.
        """
        book = self.fresh_book(leg.key, aging, when)
        if not book:
            return []
        selling = [(price, size) for price, size in sell_ladder(book, leg.polarity, leg.side) if price >= least - 1e-9]
        sold, _ = sweep(selling, excess, leg.venue, leg.fee_info, config.FILL_SHARE, selling=True, step=self.step)
        return selling if sold else []

    async def sell_excess(self, trade, leg, excess, average, selling, when=None):
        """
        Sell the excess back on its own venue, no lower than the book said
        it would sell, sent at when or now, and record what came back.
        Returns (contracts sold, note).
        """
        fill = await self.sell_back(trade, leg, excess, reach(selling, excess, config.FILL_SHARE, self.step), when)
        if fill.unfunded:
            if trade.id not in self.unfunded:
                self.log(f"{self.mode} {trade.label}: {leg.venue} lacked the cash to sell back {excess:g}, which waits until its cash grows")
            self.unfunded[trade.id] = (leg.venue, shard(leg), self.cash.available(leg.venue, shard(leg)))
            return 0, f"sold back 0 of {excess:g} on {leg.venue}, which lacked the cash"
        self.unfunded.pop(trade.id, None)
        if fill.filled:
            self.unsold.pop(trade.id, None)
            self.cash.apply(Ledger(fill.ts, leg.venue, fill.dollars, "sell", trade.id), shard(leg))
        else:
            self.unsold[trade.id] = epoch(self.clock()) + config.SALE_RETRY_SECONDS
        leg.held = exact(leg.held - fill.filled)
        leg.cost -= fill.filled * average
        trade.hedge_pnl += fill.dollars - fill.filled * average
        return fill.filled, f"sold back {fill.filled:g} of {excess:g} on {leg.venue}"

    async def flatten(self, trade, legs, when=None):
        """
        Sell the excess on the leg holding more back on its own venue, sent
        like any other order, no lower than config.MIN_SALE_SHARE of what it
        cost, at when, by our clock, on the books as they were then, or now.
        Returns what was done in words, or None when nothing would sell at
        that, its venue is not trading, or the trade has been set aside.
        """
        if trade.id in self.set_aside:
            return None
        long_leg, short_leg = sorted(legs, key=lambda leg: leg.held, reverse=True)
        if self.is_maintenance(long_leg.venue, when or self.clock()):
            return None
        excess = exact(long_leg.held - short_leg.held)
        average = long_leg.cost / long_leg.held         # What each contract the long leg holds cost.
        selling = self.sale_ladder(long_leg, excess, self.aging(trade), average * config.MIN_SALE_SHARE, when)
        if not selling:
            return None
        done, note = await self.sell_excess(trade, long_leg, excess, average, selling, when)
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
        self.unfunded.pop(trade_id, None)
        self.unsold.pop(trade_id, None)

    async def fill_legs(self, trade, legs):
        """
        Send both legs' orders and return their Fills, in the legs' order.
        Both go out at once, unless the trade has a lead in leads: then the
        leg on that venue goes first, and the other only once it has
        answered, for no more than it filled, and not at all when it filled
        nothing, so a lead that misses leaves nothing to sell back.
        """
        lead = self.leads.pop(trade.id, None)
        if lead is None:
            return list(await asyncio.gather(*(self.fill(trade, leg) for leg in legs)))
        first = next(leg for leg in legs if leg.venue == lead)
        second = next(leg for leg in legs if leg is not first)
        led = await self.fill(trade, first)
        second.quantity = exact(min(second.quantity, led.filled))
        if second.quantity < self.step:
            follow = Fill(ts=led.ts, note=f"not sent, as {lead} filled nothing first")
        else:
            follow = await self.fill(trade, second, led.ts)
        return [led, follow] if legs[0] is first else [follow, led]

    async def run_trade(self, trade, legs):
        """
        Fill both legs, see fill_legs(), flatten any mismatch as soon as both answers are in, and record the result.
        """
        reserved = [leg.quantity * leg.limit for leg in legs]
        fills = await self.fill_legs(trade, legs)
        answered = max((fill.ts for fill in fills if fill.ts), default=None)     # When the later answer came, which paper works out after.
        for leg, fill, cost in zip(legs, fills, reserved):
            self.cash.release(leg.venue, cost, shard(leg))
            leg.held, leg.cost = fill.filled, fill.dollars
            if fill.filled:
                self.cash.apply(Ledger(fill.ts, leg.venue, -fill.dollars, "buy", trade.id), shard(leg))
        self.record_fills(trade, fills)
        trade.hedge_pnl = 0.0
        hedge = None                # How the mismatch was flattened, in words, when there was one.
        if trade.yes_filled != trade.no_filled:
            exposed = exact(abs(trade.yes_filled - trade.no_filled))
            hedge = (await self.flatten(trade, legs, answered)
                     or f"{exposed:g} exposed, {self.set_aside.get(trade.id, 'nothing to sell it into')}")
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
        A trade past its payout time is left to settle as it stands, one
        whose last sale filled nothing waits config.SALE_RETRY_SECONDS, and
        one whose last sale its venue turned away for lack of cash waits until
        the cash there has grown, rather than sending the same order every tick.
        Returns whether anything was sold, which is all that changes a result.
        """
        sold = False
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
            waiting = self.unfunded.get(trade_id)
            if waiting and self.cash.available(waiting[0], waiting[1]) <= waiting[2] + 1e-9:
                continue
            if epoch(now) < self.unsold.get(trade_id, 0):
                continue
            before, held = trade.hedge_pnl, [leg.held for leg in legs]
            self.flattening.add(trade_id)
            try:
                note = await self.flatten(trade, legs)
            finally:
                self.flattening.discard(trade_id)
            if not note or [leg.held for leg in legs] == held:
                continue        # Nothing sold, so nothing to record.
            sold = True
            self.record_holdings(trade, legs)
            left = exact(abs(legs[0].held - legs[1].held))
            trade.hedge += f", then {note} at {now[11:19]}"
            database.update_trade(self.conn, trade)
            self.totals["hedge"] += trade.hedge_pnl - before
            self.log(f"{self.mode} flattened {trade.label}: {note}, {left:g} still exposed, hedge {trade.hedge_pnl:+.2f}$")
            if not left:
                self.forget(trade_id)
        return sold

    def summary(self):
        """
        One line for the trades finished since the last summary and the running totals.
        """
        recent = self.done
        self.done = []
        counts = {s: sum(1 for t in recent if t.status == s) for s in ("filled", "partial", "failed")}
        waiting = ""
        if self.held:
            waiting += f"; {len(self.held)} pairs' edges held until they lasted"
            self.held = set()
        if self.waiting:
            waiting += f"; {len(self.waiting)} pairs' edges waited for a book to catch up"
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

    def under_way(self, pair, yes, no, now):
        """
        Whether the pair's game may have started by now, judged by every
        member of the pair the scanner gives, since a Kalshi contract gives no
        kickoff, or else by the legs' members yes and no. A future never starts.
        """
        return game.started(pair.get("game_date"), pair.get("members") or (yes, no), now)

    def plays(self, pair, yes, no, now):
        """
        Whether this executor trades the pair now, by whether its game may
        have started: live, a game not yet started or a future, which never
        starts; paper, whose in_play is set, a game under way too.
        """
        return self.in_play or not self.under_way(pair, yes, no, now)

    def pays_in_time(self, hours, pair):
        """
        Whether this executor trades a bet on the pair paying out hours from now: live, one config.MIN_PAYOUT_HOURS or more away.
        """
        return hours >= config.MIN_PAYOUT_HOURS

    def pays_enough(self, edge, now, pays_at, pair):
        """
        Whether an edge is worth the capital it ties up until the bet pays at
        pays_at: when this executor trades, see pays_in_time(), and returning
        config.MIN_ANNUAL_PCT a year or more until then.
        """
        if not pays_at or not self.pays_in_time(hours_between(now, pays_at), pair):
            return False
        return annual_pct(edge, game.days_until(now, pays_at)) >= config.MIN_ANNUAL_PCT

    def hold(self, pair, now):
        """
        How many seconds the pair's edge must still last before this executor trades it, 0 when it may now: paper
        takes an edge the moment it is seen, live waits, see LiveExecutor.hold().
        """
        return 0.0

    def confirm_wait(self, yes, no, now):
        """
        How many seconds until each leg's book is current enough to trade on,
        0 when it is now, or None when a leg has no book. A leg on a venue in
        config.CONFIRM_SECONDS needs a book newer, by the venues' own clocks,
        than the other leg's last change, or else that change must be at
        least that many seconds old. A book the venue gave no time for counts
        from when it reached us.
        """
        times = []
        for member in (yes, no):
            book = self.book((member["venue"], member["contract_id"]))
            if book is None:
                return None
            times.append(book.at if book.at is not None else epoch(book.ts))
        (yes_at, no_at), clock = times, epoch(now)
        wait = 0.0
        for member, own, other in ((yes, yes_at, no_at), (no, no_at, yes_at)):
            if own < other:
                wait = max(wait, other + config.CONFIRM_SECONDS.get(member["venue"], 0) - clock)
        return wait

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
        when the edge, whether its game has started, see plays(), the time
        until the bet pays, its return a year, how long the edge has lasted,
        see hold(), both venues trading, and the balances allow, Polymarket
        US's leg first on a game under way. Returns
        True when orders were sent, so the scanner sends no more for this
        episode. The scanner's size counts every level with a positive edge,
        while the legs are sized from the levels that keep config.MIN_EDGE,
        see quantity_for(). The cost is reserved here, before anything is
        awaited, so a second signal in the same moment sees what is left.
        """
        if edge < config.MIN_EDGE:
            return False
        if not self.plays(pair, yes, no, now):
            return False
        pays_at = game.pays_at((yes, no), pair["sport"])
        if not self.pays_enough(edge, now, pays_at, pair):
            return False
        hold = self.hold(pair, now)
        if hold > 0:
            self.held.add(pair["id"])
            if self.recheck:
                self.recheck(pair["id"], hold)
            return False
        wait = self.confirm_wait(yes, no, now)
        if wait is None or wait > 0:
            self.waiting.add(pair["id"])
            if wait and self.recheck:
                self.recheck(pair["id"], wait)
            return False
        legs = [Leg(side, m["venue"], m["contract_id"], m["polarity"], fee_info=fee_infos[(m["venue"], m["contract_id"])])
                for side, m in (("yes", yes), ("no", no))]
        if any(self.is_maintenance(leg.venue, now) for leg in legs):
            return False
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
        if self.under_way(pair, yes, no, now) and any(leg.venue == "polymarket_us" for leg in legs):
            self.leads[trade.id] = "polymarket_us"
        self.spawn(self.run_trade(trade, legs))
        return True

    def tick(self, now):
        """
        Once a second from the session. Tries again to flatten what is still exposed.
        """
        if self.exposed and (self.retrying is None or self.retrying.done()):
            self.retrying = self.spawn(self.retry(now))
