"""
Record live order books for every paired contract.

Both venues push every book change over a websocket. The recorder keeps
the latest book per contract in memory and once a second writes a row for
each contract whose best bid or best ask changed, in price or in size,
since the last row. Each row still carries the top five levels. Quiet
contracts produce nothing, busy ones produce at most one row per second.

Only futures and games within GAME_WINDOW_DAYS of kickoff are recorded.
The target list is loaded once at start, so restart the recorder when new
games enter the window.

Run with:
    python3 src/record.py --sport nfl
    python3 src/record.py --sport nfl --seconds 120

For a long run on a laptop, stop the Mac from sleeping while it runs:
    caffeinate -i -s python3 src/record.py --sport nfl
"""

import argparse
import asyncio
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


def log(message):
    """
    Print a message with the current UTC time in front.
    """
    print(f"{now_iso()[11:19]} {message}")


class Recorder:
    """
    Collects book updates from both venues and writes the changed ones on a timer.
    """

    def __init__(self, conn):
        self.conn = conn
        self.latest = {}        # (venue, contract_id) maps to the newest Quote seen.
        self.written = {}       # (venue, contract_id) maps to the best levels last written to the database.
        self.updates = {"polymarket": 0, "kalshi": 0}
        self.rows_written = 0

    def on_book(self, venue, contract_id, bids, asks):
        """
        Remember the newest book for a contract. Called by the venue streams.
        """
        self.updates[venue] += 1
        self.latest[(venue, contract_id)] = Quote(venue, contract_id, now_iso(), bids[:LEVELS], asks[:LEVELS])

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
        One line with what has happened so far.
        """
        return (f"tracking {len(self.latest)} books, updates "
                f"polymarket {self.updates['polymarket']} kalshi {self.updates['kalshi']}, rows written {self.rows_written}")


async def run(conn, sport, seconds):
    """
    Start both streams and the flush timer. Stops after the given seconds, or never when zero.
    """
    now = now_iso()
    targets = database.load_recording_targets(conn, sport, now, shift(now, days=GAME_WINDOW_DAYS))
    log(f"recording {len(targets['polymarket'])} polymarket and {len(targets['kalshi'])} kalshi contracts")
    recorder = Recorder(conn)
    streams = asyncio.gather(
        polymarket.stream_books(targets["polymarket"], lambda cid, b, a: recorder.on_book("polymarket", cid, b, a), log),
        kalshi.stream_books(targets["kalshi"], lambda cid, b, a: recorder.on_book("kalshi", cid, b, a), log),
    )
    started, last_status = time.time(), time.time()
    try:
        while not seconds or time.time() - started < seconds:
            await asyncio.sleep(FLUSH_SECONDS)
            recorder.flush()
            if time.time() - last_status >= STATUS_SECONDS:
                log(recorder.status())
                last_status = time.time()
    finally:
        streams.cancel()
        try:
            await streams
        except asyncio.CancelledError:
            pass
        recorder.flush()
        log(recorder.status())


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Record live order books for paired contracts.")
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--seconds", type=int, default=0, help="stop after this many seconds, 0 means run forever")
    args = ap.parse_args()
    with database.connect() as conn:
        try:
            asyncio.run(run(conn, args.sport, args.seconds))
        except KeyboardInterrupt:
            print("stopped")
