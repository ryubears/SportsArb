"""
Settle paper trades once their contracts have resolved.

The settler keeps asking the venues how the held contracts of open trades
resolved, from the moment their game kicks off, since Kalshi settles a
prop as soon as it is decided and both venues settle the rest within
half an hour of the final whistle. A trade with no game behind it is
checked from its payout time. Each pass costs one Kalshi call per fifty
tickers and one Polymarket US call per event, and the next pass starts
SETTLE_SECONDS after the previous one began. Winning legs are paid a
dollar a contract through the shared Balances and each leg's result and
payout is written on the trade. A trade settles only once every held leg
has a result, so a venue that is slow to resolve just delays it.

A trade still exposed on one side may be settled while the executor keeps
trying to flatten it. The two stay apart: the settler skips a trade while
an order to flatten it is in flight, pays out what a trade holds after
the venues answer rather than before, and tells the executor when a trade
has settled so it stops flattening it.
"""

import asyncio
from api import kalshi, polymarket_us
from db import database
from db.models import Ledger

SETTLE_SECONDS = 30     # Seconds between passes over the open trades whose game has started.
RESULTS = {"kalshi": kalshi.results, "polymarket_us": polymarket_us.results}    # How each venue reports how a contract resolved.
RESULTS_BY_EVENT = {"kalshi": False, "polymarket_us": True}     # Whether a venue's lookup takes event ids rather than contract ids.


def leg_won(side, polarity, result):
    """
    Whether a leg pays out. Holding the side the contract pays on wins when
    the contract resolves yes, holding the other side wins when it resolves no.
    """
    return (result == "yes") == (side == polarity)


class Settler:
    """
    Pays out trades whose contracts have resolved.
    results maps a venue to a function giving how its contracts resolved.
    executor is the PaperExecutor whose exposed trades are kept apart from settlement, if trading.
    """

    def __init__(self, conn, cash, log=print, results=None, executor=None):
        self.conn = conn
        self.cash = cash
        self.log = log
        self.results = results or RESULTS
        self.executor = executor
        self.settled = []           # (Trade, realized dollars) since the last summary.
        self.running = None         # The settlement check while one runs.
        self.last_check = 0.0       # Wall clock seconds of the last settlement check.

    async def settle(self, now):
        """
        Ask the venues how the contracts of open trades whose game has
        started resolved, pay the winning legs, and write each leg's result on the trade.
        """
        due = [t for t in database.load_open_trades(self.conn) if (t.starts_at or t.pays_at) <= now]
        if not due:
            return
        wanted = {}
        for t in due:
            for side in ("yes", "no"):
                if getattr(t, f"{side}_held"):
                    wanted.setdefault(getattr(t, f"{side}_venue"), set()).add(getattr(t, f"{side}_contract"))
        results = {}
        for venue, ids in wanted.items():
            lookup = list(database.event_ids(self.conn, venue, list(ids)).values()) if RESULTS_BY_EVENT[venue] else list(ids)
            try:
                found = await asyncio.to_thread(self.results[venue], lookup)
            except Exception as e:
                self.log(f"settlement lookup failed for {venue} ({e!r}), will retry")
                continue
            results.update({(venue, cid): r for cid, r in found.items()})
        # The executor may have flattened some of these while the venues were asked, so pay out what the trades hold now.
        current = {t.id: t for t in database.load_open_trades(self.conn)}
        flattening = self.executor.flattening if self.executor else set()
        for t in (current.get(t.id) for t in due):
            if t is None or t.id in flattening:
                continue
            legs = [side for side in ("yes", "no") if getattr(t, f"{side}_held")]
            if any((getattr(t, f"{side}_venue"), getattr(t, f"{side}_contract")) not in results for side in legs):
                continue
            for side in legs:
                result, settled_at = results[(getattr(t, f"{side}_venue"), getattr(t, f"{side}_contract"))]
                won = leg_won(side, getattr(t, f"{side}_polarity"), result)
                setattr(t, f"{side}_result", result)
                setattr(t, f"{side}_payout", float(getattr(t, f"{side}_held")) if won else 0.0)
                setattr(t, f"{side}_settled_at", settled_at or now)
            t.settled_at = max(getattr(t, f"{side}_settled_at") for side in legs)
            for side in legs:
                if getattr(t, f"{side}_payout"):
                    self.cash.book(Ledger(t.settled_at, getattr(t, f"{side}_venue"), getattr(t, f"{side}_payout"), "payout", t.id))
            database.settle_trade(self.conn, t)
            if self.executor:
                self.executor.settled(t.id)
            realized = sum(getattr(t, f"{side}_payout") - getattr(t, f"{side}_cost") for side in legs)
            self.settled.append((t, realized))
            self.log(f"settled {t.label}: " + ", ".join(
                f"{getattr(t, f'{side}_venue')} {side} {getattr(t, f'{side}_result')} pays {getattr(t, f'{side}_payout'):.0f}$" for side in legs)
                + f", realized {realized:+.2f}$")

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a pass when one is due and none is running.
        """
        if clock - self.last_check >= SETTLE_SECONDS and (self.running is None or self.running.done()):
            self.last_check = clock
            self.running = asyncio.create_task(self.settle(now))

    def summary(self):
        """
        One line for the trades settled since the last summary.
        """
        settled = self.settled
        self.settled = []
        open_count = len(database.load_open_trades(self.conn))
        return f"settled: {len(settled)} trades for {sum(r for _, r in settled):+.2f}$, {open_count} still open"
