"""
Run the live process: record, scan, trade on paper or with real money, settle, and rebalance.

The recorder from record.py holds the newest book for every paired
contract, fed by the venue connections from streams.py, and once a second
writes the books whose top changed. The scanner from scan.py prices pairs
from the same in memory books as they change and stores every episode it
finds in the opportunities table. Its signals go to one Desk per mode the
run trades in. The paper desk's executor from execute/paper.py fills
against the same books with paper money from balances.py, and its
rebalancer moves paper money between the venues. The live desk's executor
from execute/live.py sends real orders with the money the venues report
through accounts.py, and its alert emails a human, through notify.py,
when the venues drift apart or live trading halts. Each desk has its own
allocator and settler, and its trades are stored with its mode, so paper
and live never mix. Both can run at once on the same signals, which shows
how far the paper fills are from real ones.

Every CATALOG_MINUTES the catalog is refreshed in a background thread,
fetch then classify then match, and the new pairs' contracts are added to
the live connections and the closed ones removed, without reconnecting.

Run with:
    python3 -m live.run --sport nfl
    python3 -m live.run --sport nfl --seconds 120 --catalog-minutes 0
    python3 -m live.run --sport nfl --skip-refresh
    python3 -m live.run --sport nfl --no-scan
    python3 -m live.run --sport nfl --no-trade
    python3 -m live.run --sport nfl --execute live
    python3 -m live.run --sport nfl --execute both
    python3 -m live.run --sport nfl --set min_edge=0.03 --set max_cap=100

For a long run on a laptop, stop the Mac from sleeping while it runs:
    caffeinate -i -s python3 -m live.run --sport nfl
"""

import argparse
import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass
from catalog import pipeline
from common.log import log, with_traceback
from common.paths import ROOT
from common.timeutil import now_iso
from db import database
from live.components import accounts, allocate, balances, notify, rebalance, scan, settle
from live.components.execute.live import LiveExecutor
from live.components.execute.paper import PaperExecutor
from live.components.record import Recorder, load_targets
from live.components.streams import Streams
from live.helper import config

CATALOG_MINUTES = 60    # How often the catalog is refreshed and subscriptions updated. Zero disables it.
EXECUTE = {"paper": ("paper",), "live": ("live",), "both": ("live", "paper")}     # What --execute trades in. Live first, so its orders go out first.


@dataclass(frozen=True)
class RunOptions:
    """
    What one run of the live process does, as the command line sets it.
    """
    sport: str = "nfl"
    seconds: float = 0                              # Stop after this many seconds, or never when zero.
    catalog_seconds: float = CATALOG_MINUTES * 60   # Between catalog refreshes, or never when zero.
    refresh_at_start: bool = True                   # Refresh the catalog before streaming, when refreshes are on.
    scan: bool = True                               # Price the books and store episodes.
    executors: tuple = ("paper",)                   # The modes that trade the scanner's signals, 'paper' and 'live', when scanning.


def code_version():
    """
    The commit the process runs, with '-dirty' when files differ from it, or 'unknown' outside a git checkout.
    """
    try:
        return subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def trading_settings():
    """
    The settings that decide what the paper trader does, in one line, so each run's log says what it ran with.
    """
    c = config
    latency = ", ".join(f"{venue} {median}ms" for venue, (median, _) in c.LATENCY_MS.items())
    return (f"settings: min edge {c.MIN_EDGE:.2f}$, fill share {c.FILL_SHARE}, rejects {c.REJECT_PROBABILITY:.0%}, "
            f"latency {latency}, cap {c.MIN_CAP} to {c.MAX_CAP} at {c.DOLLARS_PER_CAP}$ a contract, "
            f"game {c.GAME_HOURS}h + settle {c.SETTLE_HOURS}h, start balance {c.START_BALANCE:,.0f}$, "
            f"rebalance over {c.REBALANCE_DRIFT:.0%} or under {c.REBALANCE_FLOOR:,.0f}$")


def live_settings():
    """
    The settings that decide what the live trader does, in one line.
    """
    c = config
    return (f"LIVE TRADING with real money: cap {c.LIVE_MIN_CAP} to {c.LIVE_MAX_CAP} contracts, balances read every "
            f"{c.LIVE_BALANCE_SECONDS}s, halt after {c.LIVE_REJECT_LIMIT} refusals in a row or {c.LIVE_MAX_HEDGE_LOSS:,.2f}$ lost flattening, "
            f"email to rebalance over {c.REBALANCE_DRIFT:.0%} every {c.LIVE_ALERT_HOURS}h")


class Desk:
    """
    One mode of trading, paper or live: its executor, the money it trades,
    the allocator that sizes its trades, the settler that pays them out, and
    what keeps its venues funded, the paper rebalancer or the live alert.
    books is a function returning the recorder's newest books.
    """

    def __init__(self, mode, conn, books, notifier):
        self.mode = mode
        if mode == "paper":
            self.cash = balances.Balances(conn)
            self.allocator = allocate.Allocator(conn, self.cash)
            self.executor = PaperExecutor(conn, self.cash, books, log, allocator=self.allocator)
            self.keeper = rebalance.Rebalancer(conn, self.cash, log)
        elif mode == "live":
            self.cash = accounts.Accounts(log)
            self.allocator = allocate.Allocator(conn, self.cash)
            self.executor = LiveExecutor(conn, self.cash, books, log, allocator=self.allocator,
                                         alert=lambda subject, body: notifier.send("halt", subject, body))
            self.keeper = rebalance.RebalanceAlert(conn, self.cash, notifier, log)
        else:
            raise ValueError(f"unknown mode {mode!r}")
        self.settler = settle.Settler(conn, self.cash, log, executor=self.executor)

    def tick(self, now, clock):
        """
        Read the live balances when due, retry exposed trades, settle, and keep the venues funded.
        """
        if self.mode == "live":
            self.cash.tick(now, clock)
        self.executor.tick(now)
        self.settler.tick(now, clock)
        self.keeper.tick(now)

    def summaries(self, now):
        """
        One line per component about what it did since the last summary.
        """
        lines = [self.executor.summary(), self.settler.summary(), self.allocator.summary(now)]
        if self.mode == "paper" and self.keeper.summary():
            lines.append(self.keeper.summary())
        return lines


class Session:
    """
    The recorder and everything that runs on its books, wired together:
    the venue connections, the scanner, and a Desk for each mode it trades
    in. Without a scanner only the books are recorded, and without a desk
    the scanner only stores what it sees.
    """

    def __init__(self, conn, sport, with_scanner=True, executors=("paper",)):
        self.conn = conn
        self.sport = sport
        self.notifier = notify.Notifier(conn, log)
        # The executors trade against the recorder's books, which exist once the recorder does, below.
        self.desks = [Desk(mode, conn, lambda: self.recorder.latest, self.notifier) for mode in executors] if with_scanner else []
        self.scanner = scan.Scanner(conn, sport, log, [d.executor.signal for d in self.desks]) if with_scanner else None
        self.recorder = Recorder(conn, self.scanner)
        self.streams = Streams(self.recorder)
        self.last_status = self.last_summary = time.time()

    def start(self):
        """
        Open the venue connections for everything the catalog says to record.
        """
        if any(d.mode == "paper" for d in self.desks):
            log(trading_settings())
        if any(d.mode == "live" for d in self.desks):
            log(live_settings())
            if not notify.EMAIL_FILE.exists():
                log(f"no email settings in {notify.EMAIL_FILE}, alerts are only logged and stored")
        targets = load_targets(self.conn, self.sport)
        log("recording " + ", ".join(f"{len(ids)} {venue}" for venue, ids in targets.items()) + " contracts")
        if not any(targets.values()):
            log("nothing to record, run pipeline.py first")
        for venue, contract_ids in targets.items():
            self.streams.start(venue, contract_ids)
            if len(self.streams.streams[venue]) > 1:
                log(f"{venue} needs {len(self.streams.streams[venue])} connections for {len(contract_ids)} contracts")

    def tick(self):
        """
        One pass of the timer: write the changed books, price them, settle
        and rebalance, and log the status and summaries when they are due.
        """
        now = now_iso()
        self.recorder.flush()
        if self.scanner:
            self.scanner.tick(self.recorder.latest, now)
        for desk in self.desks:
            desk.tick(now, time.time())
        if self.scanner and time.time() - self.last_summary >= config.SUMMARY_SECONDS:
            self.summaries()
            self.last_summary = time.time()
        if time.time() - self.last_status >= config.STATUS_SECONDS:
            log(self.recorder.status())
            self.last_status = time.time()

    def refreshed(self):
        """
        After a catalog refresh, apply the new targets to the connections and the scanner.
        """
        log(f"subscriptions {self.streams.update(load_targets(self.conn, self.sport))}")
        if self.scanner:
            self.scanner.reload()
        for desk in self.desks:
            desk.allocator.reload()
            log(desk.allocator.summary(now_iso()))

    def summaries(self):
        """
        Log one line per component about what it did since the last summary.
        """
        if self.scanner:
            log(self.scanner.summary())
        for desk in self.desks:
            for line in desk.summaries(now_iso()):
                log(line)

    async def close(self):
        """
        Stop the connections, write what is left, finish the trades in flight
        and the emails being sent, and log the final summaries.
        """
        await self.streams.stop_all()
        self.recorder.flush()
        if self.scanner:
            self.scanner.tick({}, now_iso())
        pending = [task for desk in self.desks for task in desk.executor.tasks] + list(self.notifier.tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.summaries()
        log(self.recorder.status())


async def run(conn, options):
    """
    Refresh the catalog, start a Session, tick it every config.FLUSH_SECONDS,
    and keep the catalog fresh on a timer, as the RunOptions say. A refresh
    that fails is logged and tried again at the next interval, so a bad
    fetch never stops the recording.
    """
    sport, seconds, catalog_seconds = options.sport, options.seconds, options.catalog_seconds
    log(f"starting {sport}, code {code_version()}")
    if catalog_seconds and options.refresh_at_start:
        log("refreshing catalog before starting")
        try:
            log(await asyncio.to_thread(pipeline.refresh, sport, log))
        except Exception as e:
            log(with_traceback(f"catalog refresh failed ({e!r}), starting with the stored catalog", e))
    session = Session(conn, sport, options.scan, options.executors)
    session.start()
    started = last_catalog = time.time()
    refresh = None      # The background catalog refresh while one is running.
    try:
        while not seconds or time.time() - started < seconds:
            await asyncio.sleep(config.FLUSH_SECONDS)
            session.tick()
            if catalog_seconds and refresh is None and time.time() - last_catalog >= catalog_seconds:
                refresh = asyncio.create_task(asyncio.to_thread(pipeline.refresh, sport, log))
            if refresh is not None and refresh.done():
                if refresh.exception():
                    log(with_traceback(f"catalog refresh failed ({refresh.exception()!r}), keeping current subscriptions", refresh.exception()))
                else:
                    log(f"catalog refreshed, {refresh.result()}")
                    session.refreshed()
                refresh, last_catalog = None, time.time()
    finally:
        if refresh is not None:
            refresh.cancel()
        await session.close()


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Record live order books for paired contracts.")
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--seconds", type=int, default=0, help="stop after this many seconds, 0 means run forever")
    ap.add_argument("--catalog-minutes", type=int, default=CATALOG_MINUTES,
                    help="minutes between catalog refreshes, 0 means never refresh")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="start streaming at once from the stored catalog instead of refreshing first")
    ap.add_argument("--no-scan", action="store_true", help="record only, without the live scanner")
    ap.add_argument("--no-trade", action="store_true", help="scan without trading")
    ap.add_argument("--execute", choices=sorted(EXECUTE), default="paper",
                    help="trade on paper, with real money on the venues, or both at once on the same signals")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a setting from live/helper/config.py for this run, for example --set min_edge=0.03, repeatable")
    args = ap.parse_args()
    try:
        config.override(args.set)
    except ValueError as e:
        ap.error(str(e))
    options = RunOptions(sport=args.sport, seconds=args.seconds, catalog_seconds=args.catalog_minutes * 60,
                         refresh_at_start=not args.skip_refresh, scan=not args.no_scan,
                         executors=() if args.no_trade else EXECUTE[args.execute])
    sys.stdout.reconfigure(line_buffering=True)     # Print immediately even when output goes to a file.
    with database.connect() as conn:
        try:
            asyncio.run(run(conn, options))
        except KeyboardInterrupt:
            print("stopped")
