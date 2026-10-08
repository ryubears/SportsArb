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
as it lands, from the same in memory books. With Tapes from tape.py, every
book of a contract a paper order is in flight on is also kept on its tape.

Both venues say when they made each change: Kalshi on every delta,
Polymarket US on every book after the first of each connection. The status
line says how far behind the venue those books reached this process since
the last status line, median and 90th percentile, and how much of it was
ours, from the feed receiving a book to this process having it.

A market can stop trading while its book stays up, and moves. Each
venue's feed says when one of its markets is paused, closed, or decided,
and when it trades again, see BookStream.set_state(), and a market that
turned a live order away as not trading is left alone for
config.CLOSED_MARKET_SECONDS, or until its venue says it trades, see
refuse(). Either way its book is halted, see Book.halted, and is not
priced or traded, see pricing.fresh(). Each change is logged, but for what
a venue says of a market as its first book comes, as Polymarket US's
feed does of every expired one it carries, and the status line counts the
markets not trading.

Only games within config.GAME_WINDOW_DAYS of kickoff are recorded.
The process that runs all this is run.py.
"""

import dataclasses
import time
from collections import Counter
from common.stats import quantile
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

    def __init__(self, conn, scanner=None, tapes=None, log=print):
        self.conn = conn
        self.scanner = scanner
        self.tapes = tapes      # Tapes of the contracts paper orders are in flight on, or None.
        self.log = log
        self.books = {}         # (venue, contract_id) maps to the contract's newest Book.
        self.states = {}        # (venue, contract_id) maps to why its venue says the market is not trading, see on_state().
        self.refused = {}       # (venue, contract_id) maps to when, in seconds since 1970, a market that turned an order away as not
                                # trading may be traded again, see refuse().
        self.updates = {venue: 0 for venue in VENUES}
        self.last_update = {venue: None for venue in VENUES}       # Wall clock seconds of the newest update per venue.
        self.gaps = {venue: 0 for venue in VENUES}
        self.behind = {venue: [] for venue in VENUES}      # Seconds each book reached us after the venue's time for it, since the last status.
        self.ours = {venue: [] for venue in VENUES}        # The part of that from the feed receiving the book to this process having it.

    def halted(self, key):
        """
        Why a contract's market is not trading, as its venue says or a refused order showed, or None while it trades.
        """
        return self.states.get(key) or ("refused an order" if key in self.refused else None)

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
        book = self.books[key] = Book(venue, contract_id, ts or now_iso(), bids[:config.BOOK_LEVELS], asks[:config.BOOK_LEVELS], sent,
                                      self.halted(key))
        if self.tapes:
            self.tapes.add(key, book)
        if self.scanner and (before is None or top(before) != top(book)):
            self.scanner.on_book(venue, contract_id, self.books, book.ts)

    # MARKETS NOT TRADING

    def rebook(self, key):
        """
        Mark the contract's book halted or not, as halted() now says, and price its pairs again when that changed it.
        """
        book = self.books.get(key)
        if book is None or book.halted == self.halted(key):
            return
        book = self.books[key] = dataclasses.replace(book, halted=self.halted(key))
        if self.tapes:
            self.tapes.add(key, book)
        if self.scanner:
            self.scanner.on_book(key[0], key[1], self.books, now_iso())

    def on_state(self, venue, contract_id, why):
        """
        Note what a venue says of a market: why it is not trading, or None
        when it trades again, which also ends a refusal's wait, see refuse().
        A change is logged, but for what the venue says as the contract's
        first book comes.
        """
        key = (venue, contract_id)
        if why is None:
            self.states.pop(key, None)
            self.refused.pop(key, None)
        else:
            self.states[key] = why
        if key in self.books:
            self.log(f"{venue} {contract_id} {'trading again' if why is None else f'not trading: {why}'}")
        self.rebook(key)

    def refuse(self, venue, contract_id, now):
        """
        A live order on the contract was turned away as its market not
        trading, at now, so leave the market alone for
        config.CLOSED_MARKET_SECONDS, or until its venue says it trades.
        """
        key = (venue, contract_id)
        self.refused[key] = epoch(now) + config.CLOSED_MARKET_SECONDS
        self.log(f"{venue} {contract_id} turned an order away as not trading, left alone for {config.CLOSED_MARKET_SECONDS:g}s "
                 f"or until {venue} says it trades")
        self.rebook(key)

    def tick(self, now):
        """
        Once a second from the session. Ends the wait of each refused market whose time is up.
        """
        clock = epoch(now)
        for key in [key for key, until in self.refused.items() if until <= clock]:
            del self.refused[key]
            self.log(f"{key[0]} {key[1]} may be traded again, {config.CLOSED_MARKET_SECONDS:g}s after it turned an order away")
            self.rebook(key)

    def not_trading(self):
        """
        The markets not trading by venue and why, in words, or '' when there are none.
        """
        counts = Counter((key[0], self.halted(key)) for key in set(self.states) | set(self.refused))
        return ", ".join(f"{venue} {why} {n}" for (venue, why), n in sorted(counts.items()))

    def forget(self, venue, contract_ids, states=True):
        """
        Drop the books of contracts that are no longer recorded, or not seen
        for a while, and with states what their venue said of their
        markets, which a feed started again says only as it changes.
        """
        for contract_id in contract_ids:
            if states:
                self.states.pop((venue, contract_id), None)
            if self.books.pop((venue, contract_id), None) and self.tapes:
                self.tapes.add((venue, contract_id), None)

    def on_gap(self, venue, start_ts, end_ts, contract_ids):
        """
        Store a gap in one of a venue's connections and drop the books of the
        contracts it carries, which the scanner leaves out until the new
        connection sends them again. What the venue said of their markets
        stays, as the feed keeps it across connections.
        """
        database.insert_gap(self.conn, Gap(venue, start_ts, end_ts))
        self.gaps[venue] += 1
        self.forget(venue, contract_ids, states=False)

    def delay(self, venue):
        """
        How far behind the venue its books reached us since the last status, as words for the status line, and then forget it.
        """
        behind, ours = self.behind[venue], self.ours[venue]
        self.behind[venue], self.ours[venue] = [], []
        if not behind:
            return ""
        words = f", {1000 * quantile(behind, 0.5):.0f} ms behind the venue, {1000 * quantile(behind, 0.9):.0f} at 90%"
        return words + (f", {1000 * quantile(ours, 0.5):.0f} from us" if ours else "")

    def status(self):
        """
        One line with what has happened so far, including how long each venue has been quiet and how far behind it its books
        came, and the markets not trading, see not_trading().
        """
        parts = []
        for venue, n in self.updates.items():
            t = self.last_update[venue]
            parts.append(f"{venue} {n} (last {f'{time.time() - t:.0f}s ago' if t else 'never'}, {self.gaps[venue]} gaps{self.delay(venue)})")
        halted = self.not_trading()
        return f"tracking {len(self.books)} books, updates {', '.join(parts)}" + (f"; not trading: {halted}" if halted else "")
