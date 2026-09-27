"""
Hold the live order books of every paired contract.

Both venues push every book change over a websocket. The recorder keeps
the newest book per contract in memory, config.BOOK_LEVELS levels a side,
which is what the scanner prices and the executors trade against. Books
are not stored. When one of a venue's connections is lost the stretch
until its next connection is subscribed is stored as a gap, and the books
of the contracts that connection carries are dropped until it sends them
again, so the scanner can tell a quiet book from one that went unseen. A
venue's other connections carry on, and so do their books.

With a scanner from scan.py, every change at the top of a book is priced
as it lands, from the same in memory books.

Only futures and games within config.GAME_WINDOW_DAYS of kickoff are recorded.
The process that runs all this is run.py.
"""

import time
from common.timeutil import now_iso, shift
from common.venues import VENUES
from db import database
from db.models import Quote, Gap
from engine.helper import config


def load_targets(conn, sport):
    """
    The contracts to record right now, as {venue: [contract_id, ...]}.
    """
    now = now_iso()
    return database.load_recording_targets(conn, sport, now, shift(now, days=config.GAME_WINDOW_DAYS), list(VENUES),
                                           shift(now, hours=-config.RECORD_HOURS))


def top(quote):
    """
    The best bid and the best ask, each a [price, size] level, or None for a
    side with no orders. A side can be empty, so the first level is not
    always there to index.
    """
    return (quote.bids[0] if quote.bids else None, quote.asks[0] if quote.asks else None)


class Recorder:
    """
    Keeps the newest book of every contract from both venues. With a
    scanner, every change at the top of a book is priced as it lands.
    """

    def __init__(self, conn, scanner=None):
        self.conn = conn
        self.scanner = scanner
        self.latest = {}        # (venue, contract_id) maps to the newest Quote seen.
        self.updates = {venue: 0 for venue in VENUES}
        self.last_update = {venue: None for venue in VENUES}       # Wall clock seconds of the newest update per venue.
        self.gaps = {venue: 0 for venue in VENUES}

    def on_book(self, venue, contract_id, bids, asks):
        """
        Remember the newest book for a contract. Called by the venue streams.
        """
        self.updates[venue] += 1
        self.last_update[venue] = time.time()
        key = (venue, contract_id)
        before = self.latest.get(key)
        quote = self.latest[key] = Quote(venue, contract_id, now_iso(), bids[:config.BOOK_LEVELS], asks[:config.BOOK_LEVELS])
        if self.scanner and (before is None or top(before) != top(quote)):
            self.scanner.on_book(venue, contract_id, self.latest, quote.ts)

    def forget(self, venue, contract_ids):
        """
        Drop the books of contracts that are no longer recorded, or not seen for a while.
        """
        for contract_id in contract_ids:
            self.latest.pop((venue, contract_id), None)

    def on_gap(self, venue, start_ts, end_ts, contract_ids):
        """
        Store a gap in one of a venue's connections and drop the books of the
        contracts it carries, which the scanner leaves out until the new
        connection sends them again.
        """
        database.insert_gap(self.conn, Gap(venue, start_ts, end_ts))
        self.gaps[venue] += 1
        self.forget(venue, contract_ids)

    def status(self):
        """
        One line with what has happened so far, including how long each venue has been quiet.
        """
        parts = []
        for venue, n in self.updates.items():
            t = self.last_update[venue]
            parts.append(f"{venue} {n} (last {f'{time.time() - t:.0f}s ago' if t else 'never'}, {self.gaps[venue]} gaps)")
        return f"tracking {len(self.latest)} books, updates {', '.join(parts)}"
