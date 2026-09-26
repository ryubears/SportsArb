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
the venues' own, through Accounts from accounts.py.

Real money calls for brakes. Live trading halts, sending no more orders of
any kind until the process is restarted, when an order's fate cannot be
known, since what is held is then unknown too, when a venue refuses
config.LIVE_REJECT_LIMIT orders in a row, since one leg of every trade
would fill and be flattened at a loss, and when flattening has lost more
than config.LIVE_MAX_HEDGE_LOSS since the start. A halt is logged and
sent to the alert, and what is held is still settled.
"""

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from api import kalshi, orders, polymarket_us
from common import jsonutil
from common.timeutil import now_iso
from db import database
from db.models import Order
from live.components.execute.executor import Executor, Fill
from live.helper import config

PLACE = {"kalshi": kalshi.place_order, "polymarket_us": polymarket_us.place_order}   # How each venue takes an order.
ORDER_THREADS = 8       # Orders in flight at once. Two per trade, so a burst of signals is not held back.


class LiveExecutor(Executor):
    """
    Sends real orders for the trades the shared Executor decides on.
    place maps a venue to its place_order function. alert is called with a
    subject and a body when trading halts, to tell a human, for example by email.
    """

    mode = "live"

    def __init__(self, conn, cash, books, log=print, allocator=None, clock=now_iso, place=None, alert=None):
        super().__init__(conn, cash, books, log, allocator, clock)
        self.place = place or PLACE
        self.alert = alert
        self.threads = ThreadPoolExecutor(ORDER_THREADS, thread_name_prefix="orders")
        self.halted = None          # Why live trading stopped, once it has.
        self.rejects = {}           # Venue maps to the orders it has refused in a row.

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Take the signal as the paper executor would, unless live trading has halted.
        """
        if self.halted:
            return False
        return super().signal(pair, yes, no, edge, size, fee_infos, now)

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
        self.watch(trade, order)
        note = f"{answer.status}: {answer.note}" if answer.status in ("rejected", "error") else ""
        return Fill(answer.filled, answer.dollars, order.latency_ms, order.answered_at, note)

    # BRAKES

    def watch(self, trade, order):
        """
        Halt on an order whose fate is unknown, or on too many refusals in a row from one venue.
        """
        if order.status == "error":
            self.halt(f"order {order.id} for trade {trade.id} ({order.action} {order.quantity} {order.outcome} of {order.venue} "
                      f"{order.contract_id}) has an unknown fate: {order.note}. Check the venue for client id {order.client_id} "
                      f"and what the account holds.")
        elif order.status == "rejected":
            self.rejects[order.venue] = self.rejects.get(order.venue, 0) + 1
            if self.rejects[order.venue] >= config.LIVE_REJECT_LIMIT:
                self.halt(f"{order.venue} refused {self.rejects[order.venue]} orders in a row, the last with: {order.note}")
        else:
            self.rejects[order.venue] = 0

    def halt(self, reason):
        """
        Stop sending orders, log why, and tell a human. Only the first reason counts.
        """
        if self.halted:
            return
        self.halted = reason
        self.log(f"live trading halted: {reason}")
        if self.alert:
            self.alert("SportsArb live trading halted",
                       f"Live trading stopped at {self.clock()[:19]} UTC and sends no more orders until the process is restarted.\n\n"
                       f"{reason}\n\nWhat is held is still settled. Balances: {self.cash.summary()}.")

    async def retry(self, now):
        await super().retry(now)
        self.check_losses()

    async def run_trade(self, trade, legs):
        await super().run_trade(trade, legs)
        self.check_losses()

    def check_losses(self):
        if self.totals["hedge"] < -config.LIVE_MAX_HEDGE_LOSS:
            self.halt(f"flattening has lost {-self.totals['hedge']:.2f}$ since the start, over the limit of {config.LIVE_MAX_HEDGE_LOSS:.2f}$")

    def summary(self):
        line = super().summary()
        return line + (f"; HALTED: {self.halted}" if self.halted else "")
