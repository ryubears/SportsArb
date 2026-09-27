"""
Trade the scanner's signals with real orders on the venues.

The live executor makes the same decisions as the paper one, from the
shared Executor: which signals to take, how many contracts, the limit of
each leg, and how to flatten a leg that filled short. Only the orders
differ. Each is a real immediate or cancel limit order, sent through the
venue client's place_order() on a thread of its own, so an order never
waits behind a settlement lookup or a catalog refresh. A buy's limit is
the leg's limit, and an order that flattens sells no lower, or buys no
higher, than the deepest price the books said it would reach, so a book
that moved leaves the rest exposed for the next tick rather than filling
far from where it was priced. Every order is stored in the orders table
before it is sent and updated with the venue's answer, and the money is
the venues' own, through LiveBalances from balance/live.py.

An order whose outcome cannot be known, because no answer came, the venue
failed on its side, or its answer cannot be read, leaves what its trade
holds unknown. That trade is set aside: no more orders are sent for it,
and a human is told which order to look up on the venue. The rest of live
trading goes on. When live trading halts altogether, when it pauses new
trades on a venue running low, and what counts as a refusal, is in
brakes.py.
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
from run.components.execute.brakes import Brakes
from run.components.execute.executor import Executor, Fill
from run.helper import config

PLACE = {"kalshi": kalshi.place_order, "polymarket_us": polymarket_us.place_order}   # How each venue takes an order.
ORDER_THREADS = 8       # Orders in flight at once. Two per trade, so a burst of signals is not held back.


class LiveExecutor(Executor):
    """
    Sends real orders for the trades the shared Executor decides on.
    place maps a venue to its place_order function. alert is called with a
    kind, a subject, a body, and the time, to tell a human, for example by email.
    """

    mode = "live"

    def __init__(self, conn, cash, books, log=print, allocator=None, clock=now_iso, place=None, alert=None):
        super().__init__(conn, cash, books, log, allocator, clock)
        self.place = place or PLACE
        self.alert = alert
        self.threads = ThreadPoolExecutor(ORDER_THREADS, thread_name_prefix="orders")
        self.brakes = Brakes(conn, cash, log, alert, clock)
        self.low = set()            # Venues whose cash is under the floor new trades leave untouched.

    @property
    def halted(self):
        return self.brakes.halted

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Take the signal as the paper executor would, unless live trading has halted.
        """
        if self.halted:
            return False
        return super().signal(pair, yes, no, edge, size, fee_infos, now)

    def spendable(self, venue):
        """
        A venue's free cash less the floor new trades leave untouched.
        """
        return max(0.0, self.cash[venue] - self.brakes.floor())

    def tick(self, now):
        """
        Retry what is exposed, and log when a venue goes under the floor or comes back over it.
        """
        super().tick(now)
        floor = self.brakes.floor()
        for venue in VENUES:
            low = self.cash.read_at[venue] is not None and self.cash[venue] < floor
            if low != (venue in self.low):
                (self.low.add if low else self.low.discard)(venue)
                self.log(f"live {venue} has {self.cash[venue]:,.2f}$, " +
                         (f"under its {floor:,.2f}$ floor, so new trades wait until more arrives" if low else f"back over its {floor:,.2f}$ floor"))

    # ORDERS

    async def fill(self, trade, leg, purpose):
        return await self.send(trade, leg, purpose, "buy", leg.quantity, leg.limit)

    async def sell_back(self, trade, leg, quantity, floor):
        return await self.send(trade, leg, "flatten", "sell", quantity, floor)

    def flatten_limit(self, reached):
        """
        No higher than the deepest price the books said the order would pay.
        """
        return reached

    async def send(self, trade, leg, purpose, action, quantity, price):
        """
        Store an order, send it, store the venue's answer, and return it as a Fill.
        The outcome traded is the contract itself when the leg holds the side
        the contract pays on, and its other side otherwise.
        """
        if self.halted:
            return Fill(ts=self.clock(), note="not sent, live trading halted")
        outcome = "yes" if leg.side == leg.polarity else "no"
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

    def set_trade_aside(self, trade, order):
        """
        Send no more orders for a trade one of whose orders has an unknown outcome, and tell a human which order to look up.
        """
        if trade.id in self.set_aside:
            return
        self.set_aside[trade.id] = f"set aside, order {order.id} has an unknown outcome"
        self.log(f"live trade {trade.id} set aside: order {order.id}, {order.action} {order.quantity} {order.outcome} of {order.venue} "
                 f"{order.contract_id}, has an unknown outcome: {order.note}")
        if self.alert:
            venue_id = f", venue order id {order.venue_order_id}" if order.venue_order_id else ""
            self.alert("set_aside", f"SportsArb live trade {trade.id} set aside",
                       f"Order {order.id} of live trade {trade.id} got no answer that says what happened: {order.note}.\n\n"
                       f"It was an order to {order.action} {order.quantity} {order.outcome} of {order.venue} {order.contract_id} "
                       f"at {order.limit_price:.4f}, sent at {order.sent_at[:19]} UTC, client id {order.client_id}{venue_id}.\n\n"
                       f"No more orders are sent for the trade, since what it holds is unknown. Look the order up on the venue, "
                       f"and flatten what the trade holds by hand if it traded.\n\n"
                       f"Live trading goes on, and halts if {config.LIVE_UNKNOWN_LIMIT} of the last {config.LIVE_ORDER_WINDOW} "
                       f"orders have an unknown outcome.", self.clock())

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
        line = super().summary()
        if self.low:
            line += f"; new trades wait on {', '.join(sorted(self.low))}, under the floor"
        return line + (f"; HALTED: {self.halted}" if self.halted else "")
