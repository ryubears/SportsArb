"""
Record live order books for every paired contract.

Both venues push every book change over a websocket. The recorder keeps
the latest book per contract in memory and, when run.py asks, writes a row
for each contract whose best bid or best ask changed, in price or in size,
since the last row. Each row still carries the top five levels. Quiet
contracts produce nothing, busy ones produce at most one row per flush.
When a venue's connection is lost the stretch until the next connection is
subscribed is stored as a gap, and every book from the new connection is
written again, so the scanner can tell a quiet book from one that went
unseen.

With a scanner from scan.py, every change at the top of a book is priced
as it lands, from the same in memory books.

Only futures and games within config.GAME_WINDOW_DAYS of kickoff are recorded.
The process that runs all this is run.py.
"""

import time
from common import config
from common.timeutil import now_iso, shift
from common.venues import VENUES
from db import database
from db.models import Quote, Gap


def load_targets(conn, sport):
    """
    The contracts to record right now, as {venue: [contract_id, ...]}.
    """
    now = now_iso()
    return database.load_recording_targets(conn, sport, now, shift(now, days=config.GAME_WINDOW_DAYS), list(VENUES),
                                           shift(now, hours=-config.RECORD_HOURS))


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
        self.updates = {venue: 0 for venue in VENUES}
        self.last_update = {venue: None for venue in VENUES}       # Wall clock seconds of the newest update per venue.
        self.gaps = {venue: 0 for venue in VENUES}
        self.rows_written = 0

    def on_book(self, venue, contract_id, bids, asks):
        """
        Remember the newest book for a contract. Called by the venue streams.
        """
        self.updates[venue] += 1
        self.last_update[venue] = time.time()
        key = (venue, contract_id)
        before = self.latest.get(key)
        quote = self.latest[key] = Quote(venue, contract_id, now_iso(), bids[:config.BOOK_LEVELS], asks[:config.BOOK_LEVELS])
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
