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

Both venues say when they made each change: Kalshi on every delta,
Polymarket US on every book after the first of each connection. The status
line says how far behind the venue those books reached this process since
the last status line, median and 90th percentile, and how much of it was
ours, from the feed receiving a book to this process having it.

Only games within config.GAME_WINDOW_DAYS of kickoff are recorded.
The process that runs all this is run.py.
"""

import time
from common.timeutil import epoch, now_iso, shift
from common.venues import VENUES
from db import database
from db.models import Book, Gap
from engine.helper import config
from engine.helper.game import recorded_since


def load_targets(conn, sports):
    """
    The contracts of the sports to record right now, as {venue: [contract_id, ...]}.
    """
    now = now_iso()
    targets = {venue: [] for venue in VENUES}
    for sport in sports:
        for venue, contract_ids in database.load_recording_targets(conn, sport, now, shift(now, days=config.GAME_WINDOW_DAYS), list(VENUES),
                                                                   recorded_since(now)).items():
            targets[venue].extend(contract_ids)
    return targets


def percentile(values, share):
    """
    The value that a share of the sorted values fall at or below, such as 0.9 for the 90th percentile.
    """
    return values[min(int(share * len(values)), len(values) - 1)]


def top(book):
    """
    The best bid and the best ask, each a [price, size] level, or None for a
    side with no orders. A side can be empty, so the first level is not
    always there to index.
    """
    return (book.bids[0] if book.bids else None, book.asks[0] if book.asks else None)


class Recorder:
    """
    Keeps the newest book of every contract from both venues. With a
    scanner, every change at the top of a book is priced as it lands.
    """

    def __init__(self, conn, scanner=None):
        self.conn = conn
        self.scanner = scanner
        self.books = {}         # (venue, contract_id) maps to the contract's newest Book.
        self.updates = {venue: 0 for venue in VENUES}
        self.last_update = {venue: None for venue in VENUES}       # Wall clock seconds of the newest update per venue.
        self.gaps = {venue: 0 for venue in VENUES}
        self.behind = {venue: [] for venue in VENUES}      # Seconds each book reached us after the venue's time for it, since the last status.
        self.ours = {venue: [] for venue in VENUES}        # The part of that from the feed receiving the book to this process having it.

    def on_book(self, venue, contract_id, bids, asks, ts=None, books=1, sent=None):
        """
        Remember the newest book for a contract, which arrived at ts, now
        unless given. books is how many the feed received for it since the
        last, when a feed in a process of its own sent only the newest, and
        sent the venue's time for the newest, in seconds, when it gave one.
        """
        self.updates[venue] += books
        now = self.last_update[venue] = time.time()
        if sent is not None:
            self.behind[venue].append(now - sent)
            received = epoch(ts)
            if received is not None:
                self.ours[venue].append(now - received)
        key = (venue, contract_id)
        before = self.books.get(key)
        book = self.books[key] = Book(venue, contract_id, ts or now_iso(), bids[:config.BOOK_LEVELS], asks[:config.BOOK_LEVELS])
        if self.scanner and (before is None or top(before) != top(book)):
            self.scanner.on_book(venue, contract_id, self.books, book.ts)

    def forget(self, venue, contract_ids):
        """
        Drop the books of contracts that are no longer recorded, or not seen for a while.
        """
        for contract_id in contract_ids:
            self.books.pop((venue, contract_id), None)

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
        One line with what has happened so far, including how long each venue has been quiet and how far behind it its books came.
        """
        parts = []
        for venue, n in self.updates.items():
            t = self.last_update[venue]
            parts.append(f"{venue} {n} (last {f'{time.time() - t:.0f}s ago' if t else 'never'}, {self.gaps[venue]} gaps{self.delay(venue)})")
        return f"tracking {len(self.books)} books, updates {', '.join(parts)}"

    def delay(self, venue):
        """
        How far behind the venue its books reached us since the last status, as words for the status line, and then forget it.
        """
        behind, ours = sorted(self.behind[venue]), sorted(self.ours[venue])
        self.behind[venue], self.ours[venue] = [], []
        if not behind:
            return ""
        words = f", {1000 * percentile(behind, 0.5):.0f} ms behind the venue, {1000 * percentile(behind, 0.9):.0f} at 90%"
        return words + (f", {1000 * percentile(ours, 0.5):.0f} from us" if ours else "")
