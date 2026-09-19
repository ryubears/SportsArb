"""
Record live order books for every paired contract.

Both venues push every book change over a websocket. The recorder keeps
the latest book per contract in memory and once a second writes a row for
each contract whose best bid or best ask changed, in price or in size,
since the last row. Each row still carries the top five levels. Quiet
contracts produce nothing, busy ones produce at most one row per second.

Only futures and games within GAME_WINDOW_DAYS of kickoff are recorded.
Every CATALOG_MINUTES the recorder refreshes the catalog in a background
thread, fetch then classify then match. Contracts that are new to the
pairs table get a connection of their own, so the connections carrying
live games are never interrupted. Contracts that closed keep their dead
subscription until the next start, but their updates are ignored. Fee
schedule changes reach the fee history through the same refresh.

Run with:
    python3 src/record.py --sport nfl
    python3 src/record.py --sport nfl --seconds 120 --catalog-minutes 0

For a long run on a laptop, stop the Mac from sleeping while it runs:
    caffeinate -i -s python3 src/record.py --sport nfl
"""

import argparse
import asyncio
import pipeline
import sys
import time
from api import kalshi, polymarket
from db import database
from db.models import Quote
from util.timeutil import now_iso, shift

# Print immediately even when output goes to a file.
sys.stdout.reconfigure(line_buffering=True)

LEVELS = 5              # Price levels kept per side.
FLUSH_SECONDS = 1.0     # How often changed books are written.
STATUS_SECONDS = 60     # How often a status line is printed.
GAME_WINDOW_DAYS = 7    # Games further out than this are not recorded.
CATALOG_MINUTES = 60    # How often the catalog is refreshed and subscriptions updated. Zero disables it.

STREAMERS = {"polymarket": polymarket.stream_books, "kalshi": kalshi.stream_books}


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
    return database.load_recording_targets(conn, sport, now, shift(now, days=GAME_WINDOW_DAYS))


class Recorder:
    """
    Collects book updates from both venues and writes the changed ones on a timer.
    """

    def __init__(self, conn):
        self.conn = conn
        self.latest = {}        # (venue, contract_id) maps to the newest Quote seen.
        self.written = {}       # (venue, contract_id) maps to the best levels last written to the database.
        self.updates = {"polymarket": 0, "kalshi": 0}
        self.last_update = {"polymarket": None, "kalshi": None}     # Wall clock seconds of the newest update per venue.
        self.rows_written = 0

    def on_book(self, venue, contract_id, bids, asks):
        """
        Remember the newest book for a contract. Called by the venue streams.
        """
        self.updates[venue] += 1
        self.last_update[venue] = time.time()
        self.latest[(venue, contract_id)] = Quote(venue, contract_id, now_iso(), bids[:LEVELS], asks[:LEVELS])

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
        quiet = {v: f"{time.time() - t:.0f}s ago" if t else "never" for v, t in self.last_update.items()}
        return (f"tracking {len(self.latest)} books, updates polymarket {self.updates['polymarket']} "
                f"(last {quiet['polymarket']}) kalshi {self.updates['kalshi']} (last {quiet['kalshi']}), "
                f"rows written {self.rows_written}")


class Streams:
    """
    The websocket tasks feeding the recorder. Connections are only ever added.
    New contracts get a new task, and contracts that are no longer wanted are
    ignored rather than unsubscribed, so a live game's connection never drops.
    """

    def __init__(self, recorder, streamers=STREAMERS):
        self.recorder = recorder
        self.streamers = streamers
        self.tasks = []
        self.subscribed = {venue: set() for venue in streamers}    # Every contract any task streams.
        self.wanted = {venue: set() for venue in streamers}        # The contracts the recorder should keep.

    def start(self, venue, contract_ids):
        """
        Start one more task streaming these contracts into the recorder.
        """
        def callback(contract_id, bids, asks):
            if contract_id in self.wanted[venue]:
                self.recorder.on_book(venue, contract_id, bids, asks)
        self.tasks.append(asyncio.create_task(self.streamers[venue](list(contract_ids), callback, log)))
        self.subscribed[venue] |= set(contract_ids)
        self.wanted[venue] |= set(contract_ids)

    def update(self, targets):
        """
        Subscribe to contracts not yet streamed and stop keeping the ones that
        left the target list. Returns a summary of the changes.
        """
        changes = []
        for venue, contract_ids in targets.items():
            new = set(contract_ids) - self.subscribed[venue]
            dropped = self.wanted[venue] - set(contract_ids)
            self.wanted[venue] = set(contract_ids)
            self.recorder.forget(venue, dropped)
            if new:
                self.start(venue, new)
            if new or dropped:
                changes.append(f"{venue} +{len(new)} -{len(dropped)}")
        return ", ".join(changes) or "no changes"

    async def stop_all(self):
        """
        Cancel every task and wait for them to finish.
        """
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks = []


async def run(conn, sport, seconds, catalog_minutes):
    """
    Refresh the catalog, start both streams and the flush timer, and keep the
    catalog fresh on a timer. Stops after the given seconds, or never when zero.
    """
    recorder = Recorder(conn)
    streams = Streams(recorder)
    if catalog_minutes:
        log("refreshing catalog before starting")
        log(await asyncio.to_thread(pipeline.refresh, sport, log))
    targets = load_targets(conn, sport)
    log(f"recording {len(targets['polymarket'])} polymarket and {len(targets['kalshi'])} kalshi contracts")
    for venue, contract_ids in targets.items():
        streams.start(venue, contract_ids)
    started = last_status = last_catalog = time.time()
    refresh = None      # The background catalog refresh while one is running.
    try:
        while not seconds or time.time() - started < seconds:
            await asyncio.sleep(FLUSH_SECONDS)
            recorder.flush()
            if time.time() - last_status >= STATUS_SECONDS:
                log(recorder.status())
                last_status = time.time()
            if catalog_minutes and refresh is None and time.time() - last_catalog >= catalog_minutes * 60:
                refresh = asyncio.create_task(asyncio.to_thread(pipeline.refresh, sport, log))
            if refresh is not None and refresh.done():
                log(f"catalog refreshed, {refresh.result()}")
                log(f"subscriptions {streams.update(load_targets(conn, sport))}")
                refresh, last_catalog = None, time.time()
    finally:
        if refresh is not None:
            refresh.cancel()
        await streams.stop_all()
        recorder.flush()
        log(recorder.status())


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Record live order books for paired contracts.")
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--seconds", type=int, default=0, help="stop after this many seconds, 0 means run forever")
    ap.add_argument("--catalog-minutes", type=int, default=CATALOG_MINUTES,
                    help="minutes between catalog refreshes, 0 means never refresh")
    args = ap.parse_args()
    with database.connect() as conn:
        try:
            asyncio.run(run(conn, args.sport, args.seconds, args.catalog_minutes))
        except KeyboardInterrupt:
            print("stopped")
