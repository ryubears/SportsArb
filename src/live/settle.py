"""
Settle paper trades once their contracts have resolved.

Every SETTLE_SECONDS the settler looks for trades past their payout time,
asks each venue how the held contracts resolved, pays the winning legs a
dollar a contract through the shared Balances, and stores a Settlement per
leg with the realized result. A trade settles only once every held leg
has a result, so a venue that is slow to resolve just delays it.
"""

import asyncio
from api import kalshi, polymarket_us
from db import database
from db.models import Ledger, Settlement

SETTLE_SECONDS = 600    # How often trades past their payout time are checked with the venues.
RESULTS = {"kalshi": kalshi.results, "polymarket_us": polymarket_us.results}    # How each venue reports how a contract resolved.


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
    """

    def __init__(self, conn, cash, log=print, results=None):
        self.conn = conn
        self.cash = cash
        self.log = log
        self.results = results or RESULTS
        self.settled = []           # (Trade, realized dollars) since the last summary.
        self.running = None         # The settlement check while one runs.
        self.last_check = 0.0       # Wall clock seconds of the last settlement check.

    async def settle(self, now):
        """
        Ask the venues how the contracts of trades past their payout time
        resolved, pay the winning legs, and store a settlement per leg.
        """
        due = [t for t in database.load_open_trades(self.conn) if t.pays_at <= now]
        if not due:
            return
        wanted = {}
        for t in due:
            for side in ("yes", "no"):
                if getattr(t, f"{side}_held"):
                    wanted.setdefault(getattr(t, f"{side}_venue"), set()).add(getattr(t, f"{side}_contract"))
        results = {}
        for venue, ids in wanted.items():
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
                        self.cash.book(Ledger(settled_at, l.venue, l.payout, "payout", t.id))
                database.settle_trade(self.conn, t.id, settled_at, legs)
                realized = sum(l.realized for l in legs)
                self.settled.append((t, realized))
                self.log(f"settled {t.label}: " + ", ".join(f"{l.venue} {l.side} {l.result} pays {l.payout:.0f}$" for l in legs)
                         + f", realized {realized:+.2f}$")

    def tick(self, now, clock):
        """
        Once a second from the recorder loop, with the wall clock in seconds. Starts a check when one is due.
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
