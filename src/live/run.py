"""
Run the live process: record, scan, paper trade, settle, and rebalance.

The recorder from record.py holds the newest book for every paired
contract, fed by the venue connections from streams.py, and once a second
writes the books whose top changed. The scanner from scan.py prices pairs
from the same in memory books as they change and stores every episode it
finds in the opportunities table. The paper executor from execute.py
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

For a long run on a laptop, stop the Mac from sleeping while it runs:
    caffeinate -i -s python3 -m live.run --sport nfl
"""

import argparse
import asyncio
import sys
import time
from catalog import pipeline
from common.log import log
from common.timeutil import now_iso
from db import database
from live import balances, execute, rebalance, scan, settle
from live.record import Recorder, load_targets
from live.streams import Streams

# Print immediately even when output goes to a file.
sys.stdout.reconfigure(line_buffering=True)

FLUSH_SECONDS = 1.0     # How often changed books are written.
STATUS_SECONDS = 60     # How often a status line is printed.
CATALOG_MINUTES = 60    # How often the catalog is refreshed and subscriptions updated. Zero disables it.


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
        self.recorder = Recorder(conn)
        self.streams = Streams(self.recorder)
        trading = with_scanner and with_trading
        cash = balances.Balances(conn) if trading else None
        self.executor = execute.PaperExecutor(conn, cash, lambda: self.recorder.latest, log) if trading else None
        self.settler = settle.Settler(conn, cash, log) if trading else None
        self.rebalancer = rebalance.Rebalancer(conn, cash, log) if trading else None
        self.scanner = scan.Scanner(conn, sport, log, self.executor.signal if self.executor else None) if with_scanner else None
        self.recorder.scanner = self.scanner
        self.last_status = self.last_summary = time.time()

    def start(self):
        """
        Open the venue connections for everything the catalog says to record.
        """
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
            self.scanner.sweep(self.recorder.latest, now)
        if self.executor:
            self.executor.tick(now)
            self.settler.tick(now, time.time())
            self.rebalancer.tick(now)
        if self.scanner and time.time() - self.last_summary >= scan.SUMMARY_SECONDS:
            self.summaries()
            self.last_summary = time.time()
        if time.time() - self.last_status >= STATUS_SECONDS:
            log(self.recorder.status())
            self.last_status = time.time()

    def refreshed(self):
        """
        After a catalog refresh, apply the new targets to the connections and the scanner.
        """
        log(f"subscriptions {self.streams.update(load_targets(self.conn, self.sport))}")
        if self.scanner:
            self.scanner.reload()

    def summaries(self):
        """
        Log one line per component about what it did since the last summary.
        """
        if self.scanner:
            log(self.scanner.summary())
        if self.executor:
            log(self.executor.summary())
            log(self.settler.summary())
            if self.rebalancer.summary():
                log(self.rebalancer.summary())

    async def close(self):
        """
        Stop the connections, write what is left, finish the trades in flight, and log the final summaries.
        """
        await self.streams.stop_all()
        self.recorder.flush()
        if self.scanner:
            self.scanner.sweep({}, now_iso())
        if self.executor and self.executor.tasks:
            await asyncio.gather(*self.executor.tasks, return_exceptions=True)
        self.summaries()
        log(self.recorder.status())


async def run(conn, sport, seconds, catalog_seconds, refresh_at_start=True, with_scanner=True, with_trading=True):
    """
    Refresh the catalog, start a Session, tick it every FLUSH_SECONDS, and
    keep the catalog fresh on a timer. Stops after the given seconds, or
    never when zero. A refresh that fails is logged and tried again at the
    next interval, so a bad fetch never stops the recording.
    """
    if catalog_seconds and refresh_at_start:
        log("refreshing catalog before starting")
        try:
            log(await asyncio.to_thread(pipeline.refresh, sport, log))
        except Exception as e:
            log(f"catalog refresh failed ({e!r}), starting with the stored catalog")
    session = Session(conn, sport, with_scanner, with_trading)
    session.start()
    started = last_catalog = time.time()
    refresh = None      # The background catalog refresh while one is running.
    try:
        while not seconds or time.time() - started < seconds:
            await asyncio.sleep(FLUSH_SECONDS)
            session.tick()
            if catalog_seconds and refresh is None and time.time() - last_catalog >= catalog_seconds:
                refresh = asyncio.create_task(asyncio.to_thread(pipeline.refresh, sport, log))
            if refresh is not None and refresh.done():
                if refresh.exception():
                    log(f"catalog refresh failed ({refresh.exception()!r}), keeping current subscriptions")
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
    args = ap.parse_args()
    with database.connect() as conn:
        try:
            asyncio.run(run(conn, args.sport, args.seconds, args.catalog_minutes * 60, not args.skip_refresh, not args.no_scan, not args.no_trade))
        except KeyboardInterrupt:
            print("stopped")
