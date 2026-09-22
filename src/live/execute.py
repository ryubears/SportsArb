"""
Paper trade the scanner's signals against the live books.

When the scanner sees a pair with enough net edge, the executor pretends to
send one limit order per leg at the prices it saw. Each order arrives at
its venue after a random latency drawn from what we measured, and fills
against the book as it is at that moment, from the same in memory books
the scanner reads. A level still there fills, a level that shrank fills
partly, and a level that is gone does not fill. Only a share of the visible
size is assumed to be ours, since other takers see the same thing, and a
small share of orders is rejected outright.

When the two legs fill unevenly the executor goes flat at once. It either
sells the excess back on its own venue or buys the missing amount on the
other venue, whichever leaves more money, and books the result with fees.
Only pairs that pay out within RESOLVE_HOURS are traded, so capital turns
over daily.

Every cash movement goes to the ledger, so each venue's paper balance is
the starting balance plus its ledger, and survives a restart. Once a
trade's payout time has passed the executor asks each venue how the
contracts resolved, pays the winning legs, and stores a settlement per
leg with the realized result. Balances drift apart as games resolve, so a
weekly check moves money from the richer venue to the poorer one when
they are DRIFT apart, and a venue under FLOOR is topped up on any day.
A transfer takes TRANSFER_DAYS business days, during which the money is
on neither venue. Trades, settlements, the ledger, and transfers are all
stored, and a summary is logged with the scanner's.
"""

import asyncio
import math
import random
from api import kalshi, polymarket_us
from common.timeutil import add_business_days, now_iso, seconds_between
from datetime import datetime
from db import database
from db.models import Ledger, Settlement, Trade, Transfer
from live import fees
from live.pricing import ladder, trade_words
from live.scan import resolution_time

MIN_EDGE = 0.02             # Net dollars per contract a signal must show before orders are sent.
MAX_QUANTITY = 200          # Contracts per trade.
RESOLVE_HOURS = 24          # Only pairs paying out within this many hours are traded.
BALANCE = 5000.0            # Paper dollars per venue at the start.
FILL_SHARE = 0.5            # The share of visible size at a level assumed to be ours. Other takers get the rest.
REJECT_PROBABILITY = 0.03   # The share of orders a venue rejects outright, for rate limits and errors.
# Signal to fill latency per venue, as median milliseconds and the sigma of a lognormal draw. From us-east-1 a signed
# request round trip is about 35 ms to Kalshi and 30 ms to Polymarket US, and on top of that sit the feed's own lag
# in showing us the book and the venue's matching, so the medians are set above the round trips.
LATENCY_MS = {"kalshi": (50, 0.35), "polymarket_us": (60, 0.35)}
SETTLE_SECONDS = 600        # How often trades past their payout time are checked with the venues.
REBALANCE_WEEKDAY = 0       # Monday, when balances are compared.
DRIFT = 0.25                # A venue this far above the two venue average on the weekly check sends the excess over.
FLOOR = 500.0               # A venue below this is topped up to the average on any day.
TRANSFER_DAYS = 4           # Business days a transfer between venues takes.
RESULTS = {"kalshi": kalshi.results, "polymarket_us": polymarket_us.results}    # How each venue reports how a contract resolved.


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


def leg_won(side, polarity, result):
    """
    Whether a leg pays out. Holding the side the contract pays on wins when
    the contract resolves yes, holding the other side wins when it resolves no.
    """
    return (result == "yes") == (side == polarity)


class PaperExecutor:
    """
    Turns scanner signals into paper trades against the recorder's books.
    books is a function returning the newest quotes keyed by (venue, contract_id).
    results maps a venue to a function giving how its contracts resolved.
    """

    def __init__(self, conn, books, log=print, rng=None, results=None):
        self.conn = conn
        self.books = books
        self.log = log
        self.rng = rng or random.Random()
        self.results = results or RESULTS
        totals = database.ledger_totals(conn)
        self.balances = {venue: BALANCE + totals.get(venue, 0.0) for venue in LATENCY_MS}
        self.tasks = set()          # Trades in flight.
        self.done = []              # Trades finished since the last summary.
        self.settled = []           # Trades settled since the last summary, as (trade row, realized dollars).
        self.settling = None        # The settlement check while one runs.
        self.last_settle = 0.0      # Wall clock seconds of the last settlement check.
        self.last_check = None      # The date of the last weekly balance check.
        self.totals = {"trades": 0, "profit": 0.0, "hedge": 0.0}

    # SIGNALS

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Called by the scanner when a pair shows an edge. Sends the two legs
        when the edge, the payout time, and the balances allow. Returns True
        when orders were sent, so the scanner sends no more for this episode.
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
            levels = ladder(books[key], member["polarity"], side)
            legs.append({"side": side, "member": member, "key": key, "limit": levels[0][0], "fee_info": fee_infos[key]})
        # Ask for the share of the visible size we expect to get, so an unchanged book fills in full.
        quantity = int(min(size * FILL_SHARE, MAX_QUANTITY, *(self.balances[l["member"]["venue"]] // l["limit"] for l in legs)))
        if quantity < 1:
            return False
        for l in legs:
            l["quantity"] = quantity
            self.balances[l["member"]["venue"]] -= quantity * l["limit"]     # Reserved, trued up after the fill.
        trade = Trade(label=pair["label"], kind=pair["kind"], trade=trade_words(yes, no), signal_ts=now, edge=edge, quantity=quantity,
                      yes_venue=yes["venue"], yes_contract=yes["contract_id"], yes_polarity=yes["polarity"], yes_limit=legs[0]["limit"],
                      no_venue=no["venue"], no_contract=no["contract_id"], no_polarity=no["polarity"], no_limit=legs[1]["limit"], pays_at=pays_at)
        task = asyncio.create_task(self.run_trade(trade, legs))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

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
                break
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
                break
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
                break
            fee = fees.fee(venue, price, 1, fee_info)
            filled += take
            dollars += take * (price - fee if selling else price + fee)
            remaining -= take
        return filled, dollars

    # TRADES

    async def run_trade(self, trade, legs):
        """
        Fill both legs, flatten any mismatch, book the result.
        """
        fills = await asyncio.gather(*(self.fill(leg) for leg in legs))
        cash = []       # (venue, amount, reason) to write to the ledger once the trade has an id.
        for leg, fill in zip(legs, fills):
            venue = leg["member"]["venue"]
            self.balances[venue] += leg["quantity"] * leg["limit"] - fill.dollars     # Reservation replaced by what was paid.
            leg["held"], leg["cost"] = fill.filled, fill.dollars
            if fill.filled:
                cash.append((venue, -fill.dollars, "buy"))
        yes_fill, no_fill = fills
        trade.yes_filled, trade.yes_cost, trade.yes_latency_ms, trade.yes_fill_ts = yes_fill.filled, yes_fill.dollars, yes_fill.ms, yes_fill.ts
        trade.no_filled, trade.no_cost, trade.no_latency_ms, trade.no_fill_ts = no_fill.filled, no_fill.dollars, no_fill.ms, no_fill.ts
        trade.matched = min(yes_fill.filled, no_fill.filled)
        trade.profit = trade.matched * (1 - yes_fill.average - no_fill.average)
        trade.hedge, trade.hedge_pnl = "none", 0.0
        if yes_fill.filled != no_fill.filled:
            await self.flatten(trade, legs, fills, cash)
        trade.yes_held, trade.no_held = legs[0]["held"], legs[1]["held"]
        trade.yes_cost, trade.no_cost = legs[0]["cost"], legs[1]["cost"]
        notes = [f"{leg['side']} leg {fill.note}" for leg, fill in zip(legs, fills) if fill.note]
        trade.status = "failed" if trade.matched == 0 else "partial" if trade.matched < trade.quantity else "filled"
        if notes:
            trade.hedge = trade.hedge + ", " + ", ".join(notes) if trade.hedge != "none" else ", ".join(notes)
        database.insert_trade(self.conn, trade)
        for venue, amount, reason in cash:
            database.add_ledger(self.conn, Ledger(now_iso(), venue, amount, reason, trade.id))
        self.totals["trades"] += 1
        self.totals["profit"] += trade.profit
        self.totals["hedge"] += trade.hedge_pnl
        self.done.append(trade)
        self.log(f"paper {trade.status}: {trade.label}, {trade.trade}, wanted {trade.quantity}, filled {trade.yes_filled}/{trade.no_filled}, "
                 f"locked in {trade.profit:.2f}$, hedge {trade.hedge} {trade.hedge_pnl:+.2f}$")

    async def flatten(self, trade, legs, fills, cash):
        """
        Undo the excess on the leg that filled more, by selling it back on
        its venue or buying the missing amount on the other venue, whichever
        the books say leaves more money. The chosen order is sent like any other.
        """
        long_i = 0 if fills[0].filled > fills[1].filled else 1
        long_leg, short_leg = legs[long_i], legs[1 - long_i]
        long_fill = fills[long_i]
        excess = long_fill.filled - fills[1 - long_i].filled
        books = self.books()
        sell_value = buy_value = None
        if books.get(long_leg["key"]):
            quote = books[long_leg["key"]]
            n, dollars = self.expected(sell_ladder(quote, long_leg["member"]["polarity"], long_leg["side"]), excess,
                                       long_leg["member"]["venue"], long_leg["fee_info"], selling=True)
            sell_value = dollars - n * long_fill.average if n else None
        if books.get(short_leg["key"]):
            quote = books[short_leg["key"]]
            n, dollars = self.expected(ladder(quote, short_leg["member"]["polarity"], short_leg["side"]), excess,
                                       short_leg["member"]["venue"], short_leg["fee_info"], selling=False)
            buy_value = n * (1 - long_fill.average) - dollars if n else None
        if sell_value is None and buy_value is None:
            trade.hedge = f"{excess} exposed, no book to flatten"
            return
        if buy_value is None or (sell_value is not None and sell_value >= buy_value):
            fill = await self.sell_back(long_leg, excess)
            venue = long_leg["member"]["venue"]
            self.balances[venue] += fill.dollars
            if fill.filled:
                cash.append((venue, fill.dollars, "sell"))
            long_leg["held"] -= fill.filled
            long_leg["cost"] -= fill.filled * long_fill.average
            trade.hedge_pnl = fill.dollars - fill.filled * long_fill.average
            trade.hedge = f"sold back {fill.filled} of {excess} on {venue}"
        else:
            order = dict(short_leg, quantity=excess, limit=1.0)
            fill = await self.fill(order)
            venue = short_leg["member"]["venue"]
            self.balances[venue] -= fill.dollars
            if fill.filled:
                cash.append((venue, -fill.dollars, "buy"))
            short_leg["held"] += fill.filled
            short_leg["cost"] += fill.dollars
            trade.hedge_pnl = fill.filled * (1 - long_fill.average) - fill.dollars
            trade.hedge = f"bought {fill.filled} of {excess} on {venue}"
            trade.matched += fill.filled
        if fill.filled < excess:
            trade.hedge += f", {excess - fill.filled} exposed"

    # SETTLEMENT

    async def settle(self, now):
        """
        Ask the venues how the contracts of trades past their payout time
        resolved, pay the winning legs, and store a settlement per leg.
        """
        due = [t for t in database.load_open_trades(self.conn) if t.pays_at <= now]
        if not due:
            return
        wanted = {venue: set() for venue in LATENCY_MS}
        for t in due:
            for side in ("yes", "no"):
                if getattr(t, f"{side}_held"):
                    wanted[getattr(t, f"{side}_venue")].add(getattr(t, f"{side}_contract"))
        results = {}
        for venue, ids in wanted.items():
            if not ids:
                continue
            lookup = list(ids) if venue == "kalshi" else list(database.event_ids(self.conn, venue, list(ids)).values())
            try:
                found = await asyncio.to_thread(self.results[venue], lookup)
            except Exception as e:
                self.log(f"settlement lookup failed for {venue} ({e!r}), will retry")
                continue
            results.update({(venue, cid): r for cid, r in found.items()})
        for t in due:
            legs = []
            for side in ("yes", "no"):
                held = getattr(t, f"{side}_held")
                if not held:
                    continue
                key = (getattr(t, f"{side}_venue"), getattr(t, f"{side}_contract"))
                if key not in results:
                    break
                result, settled_at = results[key]
                cost = getattr(t, f"{side}_cost")
                payout = float(held) if leg_won(side, getattr(t, f"{side}_polarity"), result) else 0.0
                legs.append(Settlement(t.id, key[0], key[1], side, held, cost, result, payout, payout - cost, settled_at or now))
            else:
                settled_at = max(l.settled_at for l in legs)
                for l in legs:
                    if l.payout:
                        self.balances[l.venue] += l.payout
                        database.add_ledger(self.conn, Ledger(settled_at, l.venue, l.payout, "payout", t.id))
                database.settle_trade(self.conn, t.id, settled_at, legs)
                realized = sum(l.realized for l in legs)
                self.settled.append((t, realized))
                self.log(f"settled {t.label}: " + ", ".join(f"{l.venue} {l.side} {l.result} pays {l.payout:.0f}$" for l in legs)
                         + f", realized {realized:+.2f}$")

    # REBALANCING

    def rebalance(self, now):
        """
        Move money from the richer venue to the poorer one when they have
        drifted apart on the weekly check, or at any time when a venue is
        under the floor. One transfer is in flight at a time.
        """
        if database.load_transfers(self.conn, pending_only=True):
            return
        rich, poor = max(self.balances, key=self.balances.get), min(self.balances, key=self.balances.get)
        average = sum(self.balances.values()) / len(self.balances)
        excess = self.balances[rich] - average
        reason = None
        today = now[:10]
        if datetime.fromisoformat(now).weekday() == REBALANCE_WEEKDAY and self.last_check != today:
            self.last_check = today
            if excess > DRIFT * average:
                reason = "drift"
        if reason is None and self.balances[poor] < FLOOR and excess > 0:
            reason = "floor"
        if reason is None:
            return
        transfer = Transfer(rich, poor, round(excess, 2), now, add_business_days(now, TRANSFER_DAYS), reason)
        database.insert_transfer(self.conn, transfer)
        database.add_ledger(self.conn, Ledger(now, rich, -transfer.amount, "transfer_out"))
        self.balances[rich] -= transfer.amount
        self.log(f"transfer {transfer.id}: {transfer.amount:.2f}$ from {rich} to {poor} for {reason}, expected {transfer.expected_at[:16]}")

    def receive_transfers(self, now):
        """
        Credit transfers whose expected arrival has passed.
        """
        for t in database.load_transfers(self.conn, pending_only=True):
            if t.expected_at <= now:
                database.complete_transfer(self.conn, t.id, now)
                database.add_ledger(self.conn, Ledger(now, t.to_venue, t.amount, "transfer_in"))
                self.balances[t.to_venue] += t.amount
                self.log(f"transfer {t.id}: {t.amount:.2f}$ arrived at {t.to_venue}")

    # HOUSEKEEPING

    def tick(self, now, clock):
        """
        Once a second from the recorder loop, with the wall clock in seconds:
        land transfers, start a settlement check when one is due, and see
        whether a transfer should be requested.
        """
        self.receive_transfers(now)
        if clock - self.last_settle >= SETTLE_SECONDS and (self.settling is None or self.settling.done()):
            self.last_settle = clock
            self.settling = asyncio.create_task(self.settle(now))
        self.rebalance(now)

    def summary(self):
        """
        One line for what happened since the last summary and the running totals.
        """
        recent, settled = self.done, self.settled
        self.done, self.settled = [], []
        counts = {s: sum(1 for t in recent if t.status == s) for s in ("filled", "partial", "failed")}
        balances = ", ".join(f"{venue} {amount:,.0f}$" for venue, amount in self.balances.items())
        transit = sum(t.amount for t in database.load_transfers(self.conn, pending_only=True))
        return (f"paper: {len(recent)} trades ({counts['filled']} filled, {counts['partial']} partial, {counts['failed']} failed), "
                f"locked in {sum(t.profit for t in recent):.2f}$, hedges {sum(t.hedge_pnl for t in recent):+.2f}$; "
                f"settled {len(settled)} for {sum(r for _, r in settled):+.2f}$; "
                f"total {self.totals['trades']} trades, {self.totals['profit'] + self.totals['hedge']:.2f}$; "
                f"balances {balances}" + (f", {transit:,.0f}$ in transit" if transit else ""))
