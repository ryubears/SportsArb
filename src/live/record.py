"""
Record live order books for every paired contract.

Both venues push every book change over a websocket. The recorder keeps
the latest book per contract in memory and once a second writes a row for
each contract whose best bid or best ask changed, in price or in size,
since the last row. Each row still carries the top five levels. Quiet
contracts produce nothing, busy ones produce at most one row per second.
When a venue's connection is lost the stretch until the next connection is
subscribed is stored as a gap, and every book from the new
connection is written again, so the scanner can tell a quiet book from
one that went unseen.

The scanner from scan.py prices pairs from the same in memory books
as they change and stores every episode it finds in the opportunities
table. The paper executor from execute.py trades the scanner's signals
against the same books and stores every trade in the trades table.

Only futures and games within GAME_WINDOW_DAYS of kickoff are recorded.
Every CATALOG_MINUTES the recorder refreshes the catalog in a background
thread, fetch then classify then match, then adds the new pairs' contracts to the
live connections and removes the closed ones, without reconnecting.

Run with:
    python3 -m live.record --sport nfl
    python3 -m live.record --sport nfl --seconds 120 --catalog-minutes 0
    python3 -m live.record --sport nfl --skip-refresh
    python3 -m live.record --sport nfl --no-scan
    python3 -m live.record --sport nfl --no-trade

For a long run on a laptop, stop the Mac from sleeping while it runs:
    caffeinate -i -s python3 -m live.record --sport nfl
"""

import argparse
import asyncio
import sys
import time
from api import kalshi, polymarket_us
from catalog import pipeline
from common.timeutil import now_iso, shift
from db import database
from db.models import Quote, Gap
from live import execute, scan

# Print immediately even when output goes to a file.
sys.stdout.reconfigure(line_buffering=True)

LEVELS = 5              # Price levels kept per side.
FLUSH_SECONDS = 1.0     # How often changed books are written.
STATUS_SECONDS = 60     # How often a status line is printed.
GAME_WINDOW_DAYS = 7    # Games further out than this are not recorded.
GAME_HOURS = 5          # A game contract stays recorded this long after kickoff, whatever its close time says.
CATALOG_MINUTES = 60    # How often the catalog is refreshed and subscriptions updated. Zero disables it.

STREAMS = {"kalshi": kalshi.KalshiBookStream, "polymarket_us": polymarket_us.PolymarketUSBookStream}


def log(message):
    """
    Print a message with the current UTC time in front.
    """
    print(f"{now_iso()[11:19]} {message}")


def load_targets(conn, sport):
    """
    The contracts to record right now, as {venue: [contract_id, ...]}.
    """
    now = now_iso()
    return database.load_recording_targets(conn, sport, now, shift(now, days=GAME_WINDOW_DAYS), list(STREAMS),
                                           shift(now, hours=-GAME_HOURS))


class Recorder:
    """
    Collects book updates from both venues and writes the changed ones on a
    timer. With a scanner, every change at the top of a book is priced as it lands.
    """

    def __init__(self, conn, scanner=None):
        self.conn = conn
        self.scanner = scanner
        self.latest = {}        # (venue, contract_id) maps to the newest Quote seen.
        self.written = {}       # (venue, contract_id) maps to the best levels last written to the database.
        self.updates = {venue: 0 for venue in STREAMS}
        self.last_update = {venue: None for venue in STREAMS}       # Wall clock seconds of the newest update per venue.
        self.gaps = {venue: 0 for venue in STREAMS}
        self.rows_written = 0

    def on_book(self, venue, contract_id, bids, asks):
        """
        Remember the newest book for a contract. Called by the venue streams.
        """
        self.updates[venue] += 1
        self.last_update[venue] = time.time()
        key = (venue, contract_id)
        before = self.latest.get(key)
        quote = self.latest[key] = Quote(venue, contract_id, now_iso(), bids[:LEVELS], asks[:LEVELS])
        if self.scanner and (before is None or (before.bids[:1], before.asks[:1]) != (quote.bids[:1], quote.asks[:1])):
            self.scanner.on_book(venue, contract_id, self.latest, quote.ts)

    def on_gap(self, venue, start_ts, end_ts):
        """
        Store a venue's connection gap and drop what was known of its books,
        so every book from the new connection is written with a fresh time.
        """
        database.insert_gap(self.conn, Gap(venue, start_ts, end_ts))
        self.gaps[venue] += 1
        self.latest = {key: q for key, q in self.latest.items() if key[0] != venue}
        self.written = {key: best for key, best in self.written.items() if key[0] != venue}

    def forget(self, venue, contract_ids):
        """
        Drop contracts that are no longer recorded.
        """
        for contract_id in contract_ids:
            self.latest.pop((venue, contract_id), None)
            self.written.pop((venue, contract_id), None)

    def flush(self):
        """
        Write one row for every contract whose best bid or ask changed since its last row.
        """
        quotes = []
        for key, quote in list(self.latest.items()):
            best = (quote.bids[:1], quote.asks[:1])
            if self.written.get(key) == best:
                continue
            quotes.append(quote)
            self.written[key] = best
        if quotes:
            database.insert_quotes(self.conn, quotes)
            self.rows_written += len(quotes)

    def status(self):
        """
        One line with what has happened so far, including how long each venue has been quiet.
        """
        parts = []
        for venue, n in self.updates.items():
            t = self.last_update[venue]
            parts.append(f"{venue} {n} (last {f'{time.time() - t:.0f}s ago' if t else 'never'}, {self.gaps[venue]} gaps)")
        return f"tracking {len(self.latest)} books, updates {', '.join(parts)}, rows written {self.rows_written}"


class Streams:
    """
    One BookStream per venue, each on its own long lived connection. Changes
    to the wanted contracts are applied to the live connections in place.
    """

    def __init__(self, recorder, stream_classes=STREAMS):
        self.recorder = recorder
        self.stream_classes = stream_classes
        self.streams = {}
        self.tasks = {}

    def on_book(self, venue, contract_id, bids, asks):
        """
        Pass a book update to the recorder, unless the contract was removed and the feed has not caught up.
        """
        if contract_id in self.streams[venue].wanted:
            self.recorder.on_book(venue, contract_id, bids, asks)

    def start(self, venue, contract_ids):
        """
        Open a venue's connection for these contracts.
        """
        stream = self.stream_classes[venue](list(contract_ids), lambda cid, b, a: self.on_book(venue, cid, b, a), log,
                                            on_gap=lambda start_ts, end_ts: self.recorder.on_gap(venue, start_ts, end_ts))
        self.streams[venue] = stream
        self.tasks[venue] = asyncio.create_task(stream.run())

    def update(self, targets):
        """
        Add contracts that are new and remove the ones that left the target
        list, on the live connections. Returns a summary of the changes.
        """
        changes = []
        for venue, contract_ids in targets.items():
            stream = self.streams[venue]
            new, gone = set(contract_ids) - stream.wanted, stream.wanted - set(contract_ids)
            stream.add(new)
            stream.remove(gone)
            self.recorder.forget(venue, gone)
            if new or gone:
                changes.append(f"{venue} +{len(new)} -{len(gone)}")
        return ", ".join(changes) or "no changes"

    async def stop_all(self):
        """
        Cancel every connection and wait for them to finish.
        """
        for task in self.tasks.values():
            task.cancel()
        for task in self.tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks = {}


async def run(conn, sport, seconds, catalog_seconds, refresh_at_start=True, with_scanner=True, with_trading=True):
    """
    Refresh the catalog, start every stream and the flush timer, and keep the
    catalog fresh on a timer. Stops after the given seconds, or never when zero.
    A refresh that fails is logged and tried again at the next interval, so a
    bad fetch never stops the recording. With with_scanner, the live scanner
    runs on the same books and logs a summary every scan.SUMMARY_SECONDS,
    and with with_trading as well, the paper executor trades its signals.
    """
    if catalog_seconds and refresh_at_start:
        log("refreshing catalog before starting")
        try:
            log(await asyncio.to_thread(pipeline.refresh, sport, log))
        except Exception as e:
            log(f"catalog refresh failed ({e!r}), starting with the stored catalog")
    recorder = Recorder(conn)
    executor = execute.PaperExecutor(conn, lambda: recorder.latest, log) if with_scanner and with_trading else None
    scanner = scan.Scanner(conn, sport, log, executor.signal if executor else None) if with_scanner else None
    recorder.scanner = scanner
    streams = Streams(recorder)
    targets = load_targets(conn, sport)
    log("recording " + ", ".join(f"{len(ids)} {venue}" for venue, ids in targets.items()) + " contracts")
    if not any(targets.values()):
        log("nothing to record, run pipeline.py first")
    for venue, contract_ids in targets.items():
        streams.start(venue, contract_ids)
    started = last_status = last_catalog = last_summary = time.time()
    refresh = None      # The background catalog refresh while one is running.
    try:
        while not seconds or time.time() - started < seconds:
            await asyncio.sleep(FLUSH_SECONDS)
            recorder.flush()
            if scanner:
                scanner.sweep(recorder.latest, now_iso())
                if executor:
                    executor.tick(now_iso(), time.time())
                if time.time() - last_summary >= scan.SUMMARY_SECONDS:
                    log(scanner.summary())
                    if executor:
                        log(executor.summary())
                    last_summary = time.time()
            if time.time() - last_status >= STATUS_SECONDS:
                log(recorder.status())
                last_status = time.time()
            if catalog_seconds and refresh is None and time.time() - last_catalog >= catalog_seconds:
                refresh = asyncio.create_task(asyncio.to_thread(pipeline.refresh, sport, log))
            if refresh is not None and refresh.done():
                if refresh.exception():
                    log(f"catalog refresh failed ({refresh.exception()!r}), keeping current subscriptions")
                else:
                    log(f"catalog refreshed, {refresh.result()}")
                    log(f"subscriptions {streams.update(load_targets(conn, sport))}")
                    if scanner:
                        scanner.reload()
                refresh, last_catalog = None, time.time()
    finally:
        if refresh is not None:
            refresh.cancel()
        await streams.stop_all()
        recorder.flush()
        if scanner:
            scanner.sweep({}, now_iso())
            log(scanner.summary())
        if executor:
            if executor.tasks:
                await asyncio.gather(*executor.tasks, return_exceptions=True)
            log(executor.summary())
        log(recorder.status())


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
