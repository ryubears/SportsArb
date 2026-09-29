"""
Keep both venues funded: move paper money between them, and ask a human to move live money.

Balances drift apart as games resolve, because the venue holding the
winning leg receives the whole dollar and the other receives nothing.

Trades are open most of the time, so the venues are compared as they
stand, see drift(): each counts its free cash plus what open trades hold
on it, at cost, which is about what those trades will pay back there, and
the paper money already on its way to it. The excess is what the larger
has above the two venue average, and only the part of it that is free
cash above the floor can move, since money held in trades cannot.

The PaperRebalancer moves the money itself. Once a day, from
config.PAPER_REBALANCE_HOUR UTC, when the night's games have settled and
the day's have not begun, the venues are compared, and when the larger
sits more than config.REBALANCE_DRIFT above the average, what can move of
the excess is sent to the other venue. A transfer takes
config.PAPER_TRANSFER_DAYS business days. Until it lands the money cannot
be traded on either venue, but it counts for the venue it is going to, so
the checks on the days in between do not send it again. Every transfer is
stored.

Live money is moved by hand, so the LiveRebalancer only emails. It
compares the venues each minute, and when the larger sits more than
config.REBALANCE_DRIFT above the average it sends an alert saying how much
to move where, again every config.LIVE_ALERT_HOURS while they stay apart.
Any day will do, since a person decides when to move the money.
"""

from datetime import datetime
from typing import NamedTuple
from common.timeutil import add_business_days, hours_between, seconds_between
from db import database
from db.models import Ledger, Transfer
from engine.helper import config


class Drift(NamedTuple):
    """
    How far apart the venues stand, from drift().
    """
    totals: dict        # Each venue's free cash, plus what open trades hold on it at cost, plus the money on its way to it.
    held: dict          # What open trades hold on each venue, at cost.
    incoming: dict      # Paper money on its way to each venue, in transfers that have not landed.
    average: float      # The average of the totals.
    rich: str           # The venue with the largest total.
    poor: str           # The venue with the smallest total.
    excess: float       # What the rich venue's total has above the average.
    movable: float      # The part of the excess the rich venue has free above its floor.

    def apart(self):
        """
        Whether the venues are far enough apart to move money, and some of it can move.
        """
        return self.excess > config.REBALANCE_DRIFT * self.average and self.movable > 0

    def standing(self):
        """
        The totals in one phrase, with what each venue has in open trades and on its way, for alerts and log lines.
        """
        phrases = []
        for venue, total in self.totals.items():
            parts = [f"{self.held[venue]:,.0f}$ of it in open trades"] if self.held.get(venue) else []
            parts += [f"{self.incoming[venue]:,.0f}$ on its way"] if self.incoming.get(venue) else []
            phrases.append(f"{venue} {total:,.0f}$" + (f" ({', '.join(parts)})" if parts else ""))
        return ", ".join(phrases)


def drift(conn, cash, incoming=None):
    """
    How far apart the venues stand, as a Drift. A leg cost about what it
    will pay back, since its price is about its chance of paying, so each
    venue's free cash plus what open trades hold on it at cost is about
    what it will have once they settle. incoming is the money on its way
    to each venue, which it will have once it lands.
    """
    held = database.load_held(conn, cash.mode)
    incoming = incoming or {}
    totals = {venue: cash[venue] + held.get(venue, 0.0) + incoming.get(venue, 0.0) for venue in cash.amounts}
    average = sum(totals.values()) / len(totals)
    rich, poor = max(totals, key=totals.get), min(totals, key=totals.get)
    excess = totals[rich] - average
    return Drift(totals, held, incoming, average, rich, poor, excess, min(excess, cash.spendable(rich)))


class PaperRebalancer:
    """
    Requests transfers when the balances call for one and lands them when their time comes.
    """

    def __init__(self, conn, cash, log=print):
        self.conn = conn
        self.cash = cash
        self.log = log
        self.last_check = None      # The date of the last daily check.

    def incoming(self):
        """
        Dollars on their way to each venue, in transfers that have not landed, as {venue: dollars}.
        """
        coming = {}
        for t in database.load_transfers(self.conn, pending_only=True):
            coming[t.to_venue] = coming.get(t.to_venue, 0.0) + t.amount
        return coming

    def rebalance(self, now):
        """
        Once a day, from config.PAPER_REBALANCE_HOUR UTC, request a transfer
        from the richer venue to the poorer one when they have drifted apart,
        of what can move of the excess.
        """
        today = now[:10]
        if datetime.fromisoformat(now).hour < config.PAPER_REBALANCE_HOUR or self.last_check == today:
            return
        self.last_check = today
        d = drift(self.conn, self.cash, self.incoming())
        if not d.apart():
            return
        transfer = Transfer(d.rich, d.poor, round(d.movable, 2), now, add_business_days(now, config.PAPER_TRANSFER_DAYS), "drift")
        database.insert_transfer(self.conn, transfer)
        self.cash.apply(Ledger(now, d.rich, -transfer.amount, "transfer_out"))
        self.log(f"transfer {transfer.id}: {transfer.amount:.2f}$ from {d.rich} to {d.poor} for drift, expected {transfer.expected_at[:16]}, "
                 f"with {d.standing()}")

    def receive(self, now):
        """
        Credit transfers whose expected arrival has passed.
        """
        for t in database.load_transfers(self.conn, pending_only=True):
            if t.expected_at <= now:
                database.complete_transfer(self.conn, t.id, now)
                self.cash.apply(Ledger(now, t.to_venue, t.amount, "transfer_in"))
                self.log(f"transfer {t.id}: {t.amount:.2f}$ arrived at {t.to_venue}")

    def tick(self, now):
        """
        Once a second from the session.
        """
        self.receive(now)
        self.rebalance(now)

    def summary(self):
        """
        One line about money in transit, or None when there is none.
        """
        pending = database.load_transfers(self.conn, pending_only=True)
        if not pending:
            return None
        return "transfers: " + ", ".join(f"{t.amount:,.0f}$ {t.from_venue} to {t.to_venue}, due {t.expected_at[:10]}" for t in pending)


class LiveRebalancer:
    """
    Emails a human through the notifier when the live venues have drifted apart. Live money is never moved here.
    """

    CHECK_SECONDS = 60      # Between comparisons of the balances.

    def __init__(self, conn, cash, notifier, log=print):
        self.conn = conn
        self.cash = cash
        self.notifier = notifier
        self.log = log
        self.last_check = None      # When the balances were last compared, ISO 8601 UTC.

    def check(self, now):
        """
        Send an alert when the venues are apart, every venue has been read, and none was sent lately.
        """
        if not all(self.cash.read_at.values()):
            return None
        d = drift(self.conn, self.cash)
        if not d.apart():
            return None
        last = database.last_alert_ts(self.conn, "rebalance")
        if last and hours_between(last, now) < config.LIVE_ALERT_HOURS:
            return None
        rest = ("" if d.movable == d.excess else
                f" The rest of its {d.excess:,.2f}$ excess is held in open trades and cannot move until they settle.")
        body = (f"The live venues have drifted apart, counting what open trades hold on each at cost: {d.standing()}, "
                f"an average of {d.average:,.2f}$.\n\n"
                f"Move {d.movable:,.2f}$ from {d.rich} to {d.poor}. {d.rich} is {100 * d.excess / d.average:.0f}% above the average, "
                f"past the {100 * config.REBALANCE_DRIFT:.0f}% that calls for a transfer.{rest}\n\n"
                f"Until the money arrives, {d.poor} limits how much each live trade can hold.")
        return self.notifier.send("rebalance", f"SportsArb: move {d.movable:,.0f}$ from {d.rich} to {d.poor}", body, now)

    def tick(self, now):
        """
        Once a second from the session. Compares the balances once a minute.
        """
        if self.last_check is None or seconds_between(self.last_check, now) >= self.CHECK_SECONDS:
            self.last_check = now
            self.check(now)
