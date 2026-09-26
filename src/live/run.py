"""
Run the live process: record, scan, paper trade, settle, and rebalance.

The recorder from record.py holds the newest book for every paired
contract, fed by the venue connections from streams.py, and once a second
writes the books whose top changed. The scanner from scan.py prices pairs
from the same in memory books as they change and stores every episode it
finds in the opportunities table. The paper executor from execute/paper.py
trades the scanner's signals against the same books, settle.py pays the
trades out when their contracts resolve, and rebalance.py keeps the two
paper balances level.

Every CATALOG_MINUTES the catalog is refreshed in a background thread,
fetch then classify then match, and the new pairs' contracts are added to
the live connections and the closed ones removed, without reconnecting.

Run with:
    python3 -m live.run --sport nfl
    python3 -m live.run --sport nfl --seconds 120 --catalog-minutes 0
    python3 -m live.run --sport nfl --skip-refresh
    python3 -m live.run --sport nfl --no-scan
    python3 -m live.run --sport nfl --no-trade
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
from live.components import allocate, balances, rebalance, scan, settle
from live.components.execute.paper import PaperExecutor
from live.components.record import Recorder, load_targets
from live.components.streams import Streams
from live.helper import config

CATALOG_MINUTES = 60    # How often the catalog is refreshed and subscriptions updated. Zero disables it.


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
    trade: bool = True                              # Paper trade the scanner's signals, when scanning.


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


class Session:
    """
    The recorder and everything that runs on its books, wired together:
    the venue connections, the scanner, and the paper executor with its
    settler and rebalancer. Without a scanner only the books are recorded,
    and without trading the scanner only stores what it sees.
    """

    def __init__(self, conn, sport, with_scanner=True, with_trading=True):
        self.conn = conn
        self.sport = sport
        trading = with_scanner and with_trading
        cash = balances.Balances(conn) if trading else None
        self.allocator = allocate.Allocator(conn, cash) if trading else None
        # The executor trades against the recorder's books, which exist once the recorder does, below.
        self.executor = PaperExecutor(conn, cash, lambda: self.recorder.latest, log, allocator=self.allocator) if trading else None
        self.settler = settle.Settler(conn, cash, log, executor=self.executor) if trading else None
        self.rebalancer = rebalance.Rebalancer(conn, cash, log) if trading else None
        self.scanner = scan.Scanner(conn, sport, log, self.executor.signal if self.executor else None) if with_scanner else None
        self.recorder = Recorder(conn, self.scanner)
        self.streams = Streams(self.recorder)
        self.last_status = self.last_summary = time.time()

    def start(self):
        """
        Open the venue connections for everything the catalog says to record.
        """
        if self.executor:
            log(trading_settings())
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
        if self.executor:
            self.executor.tick(now)
            self.settler.tick(now, time.time())
            self.rebalancer.tick(now)
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
        if self.allocator:
            self.allocator.reload()
            log(self.allocator.summary(now_iso()))

    def summaries(self):
        """
        Log one line per component about what it did since the last summary.
        """
        if self.scanner:
            log(self.scanner.summary())
        if self.executor:
            log(self.executor.summary())
            log(self.settler.summary())
            log(self.allocator.summary(now_iso()))
            if self.rebalancer.summary():
                log(self.rebalancer.summary())

    async def close(self):
        """
        Stop the connections, write what is left, finish the trades in flight, and log the final summaries.
        """
        await self.streams.stop_all()
        self.recorder.flush()
        if self.scanner:
            self.scanner.tick({}, now_iso())
        if self.executor and self.executor.tasks:
            await asyncio.gather(*self.executor.tasks, return_exceptions=True)
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
    session = Session(conn, sport, options.scan, options.trade)
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
    ap.add_argument("--no-trade", action="store_true", help="scan without paper trading")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a setting from live/helper/config.py for this run, for example --set min_edge=0.03, repeatable")
    args = ap.parse_args()
    try:
        config.override(args.set)
    except ValueError as e:
        ap.error(str(e))
    options = RunOptions(sport=args.sport, seconds=args.seconds, catalog_seconds=args.catalog_minutes * 60,
                         refresh_at_start=not args.skip_refresh, scan=not args.no_scan, trade=not args.no_trade)
    sys.stdout.reconfigure(line_buffering=True)     # Print immediately even when output goes to a file.
    with database.connect() as conn:
        try:
            asyncio.run(run(conn, options))
        except KeyboardInterrupt:
            print("stopped")
