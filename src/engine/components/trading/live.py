"""
Trade the scanner's signals with real orders on the venues.

The live executor makes the same decisions as the paper one, from the
shared Executor: which signals to take, how many contracts, the limit of
each leg, and how to flatten a leg that filled short. Only the orders
differ. Each is a real immediate or cancel limit order, sent through the
venue client's place_order() on a thread of its own, so an order never
waits behind a settlement lookup or a catalog refresh. A buy's limit is
the leg's limit, and an order that flattens sells no lower than the
deepest price the books said it would reach, so a book that moved leaves
the rest exposed for the next tick rather than filling far from where it
was priced. Every order is stored in the orders table before it is sent
and updated with the venue's answer, and the money is the venues' own,
through LiveBalances from money/live.py.

An order whose outcome cannot be known, because no answer came, the venue
failed on its side, or its answer cannot be read, leaves what its trade
holds unknown. That trade is set aside: no more orders are sent for it,
and the log says which order to look up on the venue. No email goes out
for one, but enough of them halt live trading, and the email about the
halt names them all. The rest of live trading goes on. When live trading
halts, whether flattening goes on then, and what counts as a refusal, is
in brakes.py.
"""

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from api import kalshi, orders, polymarket_us
from common import jsonutil
from common.timeutil import now_iso
from common.venues import VENUES
from db import database
from db.models import Order
from engine.components.trading.brakes import Brakes
from engine.components.trading.executor import Executor, Fill
from engine.helper import config

PLACE = {"kalshi": kalshi.place_order, "polymarket_us": polymarket_us.place_order}   # How each venue takes an order.
ORDER_THREADS = 8       # Orders in flight at once. Two per trade, so a burst of signals is not held back.


class LiveExecutor(Executor):
    """
    Sends real orders for the trades the shared Executor decides on.
    place maps a venue to its place_order function. notifier is the Notifier
    from notify.py, which emails a human when live trading halts and when a
    venue's cash, or one of its shards' in config.LIVE_SHARDS, runs low.
    """

    mode = "live"

    def __init__(self, conn, cash, books, log=print, clock=now_iso, place=None, notifier=None):
        super().__init__(conn, cash, books, log, clock)
        self.place = place or PLACE
        self.threads = ThreadPoolExecutor(ORDER_THREADS, thread_name_prefix="orders")
        self.brakes = Brakes(conn, cash, log, notifier, clock)
        self.notifier = notifier
        self.low = set()            # (venue, shard or None) whose cash is under config.LIVE_LOW_CASH, once a human has been told.

    @property
    def halted(self):
        return self.brakes.halted

    def watch_cash(self, venue, part, now):
        """
        Email once when the cash on a venue, or on its shard part, falls under config.LIVE_LOW_CASH, and log when it is back over.
        """
        dollars = self.cash.available(venue, part)
        where = venue if part is None else f"{venue} shard {part}"
        if dollars < config.LIVE_LOW_CASH and (venue, part) not in self.low:
            self.low.add((venue, part))
            subject = f"SportsArb live {where} cash low: {dollars:,.2f}$"
            body = (f"Live cash on {where} is {dollars:,.2f}$, under {config.LIVE_LOW_CASH:,.2f}$. Trades there go on as far as it "
                    f"pays for, so add money to the venue to keep trading"
                    + (", or move some to the shard with python3 -m tools.kalshi_shards." if part is not None else "."))
            if self.notifier:
                self.notifier.send("low_cash", subject, body, now)
            else:
                self.log(f"live {where} has {dollars:,.2f}$, under {config.LIVE_LOW_CASH:,.2f}$")
        elif dollars >= config.LIVE_LOW_CASH and (venue, part) in self.low:
            self.low.discard((venue, part))
            self.log(f"live {where} has {dollars:,.2f}$, back over {config.LIVE_LOW_CASH:,.2f}$")

    def tick(self, now):
        """
        The shared tick, and an email once when a venue's cash falls under
        config.LIVE_LOW_CASH, again only after it has been back over. A venue
        with shards in config.LIVE_SHARDS is watched shard by shard, since an
        order spends only its own shard's cash.
        """
        super().tick(now)
        for venue in VENUES:
            if not self.cash.known(venue):
                continue
            for part in config.LIVE_SHARDS.get(venue) or (None,):
                self.watch_cash(venue, part, now)

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Take the signal as the paper executor would, unless live trading has halted.
        """
        if self.halted:
            return False
        return super().signal(pair, yes, no, edge, size, fee_infos, now)

    # ORDERS

    def set_trade_aside(self, trade, order):
        """
        Send no more orders for a trade one of whose orders has an unknown
        outcome, and log which order to look up on the venue, with the ids it
        is found by there.
        """
        if trade.id in self.set_aside:
            return
        self.set_aside[trade.id] = f"set aside, order {order.id} has an unknown outcome"
        venue_id = f", venue order id {order.venue_order_id}" if order.venue_order_id else ""
        self.log(f"live trade {trade.id} set aside: order {order.id}, {order.action} {order.quantity} {order.outcome} of {order.venue} "
                 f"{order.contract_id} at {order.limit_price:.4f}, client id {order.client_id}{venue_id}, has an unknown outcome: "
                 f"{order.note}. Look it up on the venue and flatten the trade by hand if it traded.")

    async def send(self, trade, leg, purpose, action, quantity, price):
        """
        Store an order, send it, store the venue's answer, and return it as a Fill.
        The order trades the outcome of the contract the leg holds, see Leg.outcome.
        """
        if self.brakes.stopped or (purpose == "open" and self.halted):
            return Fill(ts=self.clock(), note="not sent, live trading halted")
        outcome = leg.outcome
        order = Order(trade_id=trade.id, venue=leg.venue, contract_id=leg.contract_id, purpose=purpose, action=action, outcome=outcome,
                      quantity=quantity, limit_price=price, client_id=str(uuid.uuid4()), sent_at=self.clock())
        database.insert_order(self.conn, order)
        started = time.perf_counter()
        try:
            answer = await asyncio.get_running_loop().run_in_executor(
                self.threads, self.place[leg.venue], leg.contract_id, action, outcome, quantity, price, order.client_id)
        except Exception as e:
            answer = orders.unknown(e)          # The venue may have taken it before its answer could not be read.
        order.latency_ms = int((time.perf_counter() - started) * 1000)
        order.answered_at = self.clock()
        order.status, order.venue_order_id, order.filled, order.dollars, order.fees, order.note = (
            answer.status, answer.order_id, answer.filled, answer.dollars, answer.fees, answer.note)
        order.response = jsonutil.dump(answer.response)
        database.update_order(self.conn, order)
        if order.status == "error":
            self.set_trade_aside(trade, order)
        self.brakes.watch(order)
        note = f"{answer.status}: {answer.note}" if answer.note else ""
        return Fill(answer.filled, answer.dollars, order.latency_ms, order.answered_at, note)

    async def fill(self, trade, leg):
        return await self.send(trade, leg, "open", "buy", leg.quantity, leg.limit)

    async def sell_back(self, trade, leg, quantity, floor):
        return await self.send(trade, leg, "flatten", "sell", quantity, floor)

    # RESULTS, which the brakes check whenever a trade may have been decided.

    async def run_trade(self, trade, legs):
        await super().run_trade(trade, legs)
        self.brakes.check_results()

    async def retry(self, now):
        await super().retry(now)
        self.brakes.check_results()

    def settled(self, trade_id):
        super().settled(trade_id)
        self.brakes.check_results()

    def summary(self):
        if self.brakes.stopped:
            return super().summary() + f"; HALTED, no orders at all: {self.brakes.stopped}"
        return super().summary() + (f"; HALTED, still flattening: {self.halted}" if self.halted else "")
