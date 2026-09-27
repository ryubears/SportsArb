"""
When live trading halts.

Real money calls for brakes, sized for a test with about 100 dollars on
each venue. Each rule reads the orders and trades tables, so a restart does
not reset what it has seen, and each starts over after a halt, so what led
to one does not halt live trading again once a human has cleared it.

A halt stops every order, new trades and flattening alike, until a human
has checked the venues, removed HALT_FILE, and restarted the process. What
is held is still settled. Live trading halts when:

- config.LIVE_UNKNOWN_LIMIT of the last config.LIVE_ORDER_WINDOW orders had
  an unknown outcome: no answer, a venue failing on its side, or an answer
  that cannot be read. That is three in a row, or a steady error rate. One
  such order only sets its own trade aside, see live.py.
- One venue refused its last config.LIVE_REJECT_LIMIT orders. A refusal is
  the venue answering that it will not take an order, so nothing traded:
  an error status in the 400s, such as not authorized, not enough money,
  a bad price, or too many requests, or Polymarket US rejecting the order,
  for example because the market has closed. An order it took that found
  nothing at its price is unfilled, not refused, and so is one that
  Polymarket US turned away for having no liquidity or for being slow.
- The live trades decided in the last config.LIVE_RESULT_HOURS lost more
  than config.LIVE_MAX_LOSS_SHARE of the live money, net.
- At least config.LIVE_MIN_RESULTS trades were decided in that window
  with a profit or a loss, and config.LIVE_MAX_LOSING_SHARE or more of them
  lost, so a quiet stretch with few trades never halts on its own.

A trade is decided once its result no longer depends on the game: when
both legs hold the same number of contracts, which pay a dollar each
whichever way the game goes, or once it settles. Its result is what it is
paid, plus what it sold, less what it bought, fees included, from the
orders table. A trade with an order of unknown outcome is left out, since
its records may be wrong.

The live money a loss is measured against is the cash on both venues
plus what open live trades hold, at what it cost.
"""

from common.paths import DATA_DIR
from common.timeutil import now_iso, shift
from db import database
from engine.helper import config

HALT_FILE = DATA_DIR / "live_halted.txt"    # Why live trading halted, kept until a human removes it.


class Brakes:
    """
    Decides when live trading halts, from the orders and trades stored.
    notifier is the Notifier from notify.py that tells a human when live trading halts.
    """

    def __init__(self, conn, cash, log=print, notifier=None, clock=now_iso):
        self.conn = conn
        self.cash = cash
        self.log = log
        self.notifier = notifier
        self.clock = clock
        self.halted = None                                      # Why live trading stopped, once it has.
        self.since = database.last_alert_ts(conn, "halt")       # When live trading last halted. The rules count only what came after.
        if HALT_FILE.exists():
            self.halted = f"halted before this start, remove {HALT_FILE} to resume: {HALT_FILE.read_text().strip()}"
            self.log(f"live trading {self.halted}")

    # HALTING

    def halt(self, reason):
        """
        Stop sending orders, keep the reason in HALT_FILE, log it, and tell a human. Only the first reason counts.
        """
        if self.halted:
            return
        now = self.clock()
        self.halted, self.since = reason, now
        HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HALT_FILE.write_text(f"{now[:19]} UTC {reason}\n")
        self.log(f"live trading halted: {reason}")
        if self.notifier:
            self.notifier.send("halt", "SportsArb live trading halted",
                               f"Live trading stopped at {now[:19]} UTC and sends no more orders.\n\n{reason}\n\n"
                               f"What is held is still settled. Balances: {self.cash.summary()}.\n\n"
                               f"Once the venues are checked, remove {HALT_FILE} and restart the process to resume.", now)

    # ORDERS

    def watch(self, order):
        """
        After each answered order, halt on too many unknown outcomes, or on a venue refusing too many orders in a row.
        """
        if order.status == "error":
            statuses = database.recent_order_statuses(self.conn, config.LIVE_ORDER_WINDOW, since=self.since)
            unknown = statuses.count("error")
            if unknown >= config.LIVE_UNKNOWN_LIMIT:
                self.halt(f"{unknown} of the last {len(statuses)} orders had an unknown outcome, "
                          f"the last order {order.id} on {order.venue}: {order.note}")
        elif order.status == "rejected":
            statuses = database.recent_order_statuses(self.conn, config.LIVE_REJECT_LIMIT, venue=order.venue, since=self.since)
            if len(statuses) >= config.LIVE_REJECT_LIMIT and set(statuses) == {"rejected"}:
                self.halt(f"{order.venue} refused {len(statuses)} orders in a row, the last with: {order.note}")

    # MONEY

    def capital(self):
        """
        The live money in all: the cash on both venues, what is held back for orders in flight included, and what open live trades hold, at cost.
        """
        return self.cash.total() + database.load_open_cost(self.conn, self.cash.mode)

    # RESULTS

    def results(self, since):
        """
        The results in dollars of the live trades decided at or after since.
        """
        out = []
        for t in database.load_trade_cash(self.conn, self.cash.mode, since):
            if t["unknown"]:
                continue            # Its records may be wrong until a human has looked.
            if t["settled_at"]:
                result, decided_at = t["payouts"] + t["sold"] - t["bought"], t["settled_at"]
            elif t["yes_held"] == t["no_held"]:
                result, decided_at = t["yes_held"] + t["sold"] - t["bought"], t["last_answer"]
            else:
                continue            # Still exposed, so its result waits on the game.
            if decided_at >= since:
                out.append(result)
        return out

    def check_results(self):
        """
        After a trade may have been decided, halt on too big a loss, or too many losing trades, over the window.
        """
        if self.halted:
            return
        since = max(shift(self.clock(), hours=-config.LIVE_RESULT_HOURS), self.since or "")
        results = self.results(since)
        net = sum(results)
        before = self.capital() - net           # The live money before these results.
        if net < 0 and before > 0 and -net / before > config.LIVE_MAX_LOSS_SHARE:
            self.halt(f"the live trades decided in the last {config.LIVE_RESULT_HOURS} hours lost {-net:,.2f}$, "
                      f"{-net / before:.0%} of the live money, over the {config.LIVE_MAX_LOSS_SHARE:.0%} limit")
            return
        decided = [r for r in results if abs(r) >= 0.005]
        losing = sum(1 for r in decided if r < 0)
        if len(decided) >= config.LIVE_MIN_RESULTS and losing / len(decided) >= config.LIVE_MAX_LOSING_SHARE:
            self.halt(f"{losing} of the {len(decided)} live trades decided in the last {config.LIVE_RESULT_HOURS} hours lost money, "
                      f"at or over the {config.LIVE_MAX_LOSING_SHARE:.0%} limit")
