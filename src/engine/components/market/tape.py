"""
Keep every book of the contracts paper orders are in flight on.

A real order meets the venue's book as it is when the order reaches the
venue, and both venues stamp their book changes with their own clock, the
one they stamp an order with: Kalshi every delta, Polymarket US every book
after a connection's first. Our copy of a book runs behind the venue's,
Kalshi's by some 12 ms and Polymarket US's by some 85, so our newest book
when a paper order would arrive is the venue's book of some time before.
A Tape keeps each book of one contract that reaches us while it is open,
so paper trading can look up the book the venue had at a moment, once a
book it made later has reached us and shows that every change up to then
has, and the book we had seen by a moment, which is what live trading
decides a flatten on. See trading/paper.py.

The recorder hands each book it keeps to Tapes, which adds it to the
contract's tape when one is open and lets it go otherwise. A book the
recorder drops, as when a connection is lost, goes on a tape as None.
"""

import asyncio
import time
from contextlib import contextmanager
from common.timeutil import epoch


def later(received, book, moment):
    """
    Whether the venue made a book that reached us at received after moment,
    its clock in seconds. One it gave no time for, such as a new
    connection's first, was made by when it reached us, which is taken as
    its time. A dropped book, None, was made by no one.
    """
    return book is not None and (book.at if book.at is not None else received) > moment


class Tape:
    """
    One contract's books from when the tape was started, oldest first, each with when it reached us.
    """

    def __init__(self, book):
        self.books = []             # (our clock in seconds when it reached us, Book or None), oldest first.
        self.changed = asyncio.Event()
        self.add(book)

    def add(self, book):
        self.books.append((epoch(book.ts) if book else time.time(), book))
        self.changed.set()

    def seen(self, moment):
        """
        The newest book that had reached us by moment, our clock in seconds, or the first when none had.
        """
        found = self.books[0][1]
        for received, book in self.books[1:]:
            if received > moment:
                break
            found = book
        return found

    def at(self, moment):
        """
        The book the venue had at moment, its clock in seconds: the newest
        before the first it made later, or the first when it made that later.
        """
        found = self.books[0][1]
        for received, book in self.books[1:]:
            if later(received, book, moment):
                break
            found = book
        return found

    def complete(self, moment):
        """
        Whether every book the venue made by moment has reached us, which a book it made later shows.
        """
        return any(later(received, book, moment) for received, book in self.books)

    async def wait(self, moment, seconds):
        """
        Wait until every book the venue made by moment has reached us, or for seconds at most.
        """
        deadline = time.monotonic() + seconds
        while not self.complete(moment):
            left = deadline - time.monotonic()
            if left <= 0:
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), left)
            except TimeoutError:
                return


class Tapes:
    """
    The open tapes, one per contract, shared by the orders on it.
    """

    def __init__(self):
        self.open = {}              # (venue, contract_id) maps to [its Tape, how many are using it].

    @contextmanager
    def follow(self, key, book):
        """
        Keep a tape of a contract's books, starting from book, its newest,
        while the with block runs. One already open is shared, and it goes
        once the last user is done.
        """
        entry = self.open.get(key)
        if entry is None:
            entry = self.open[key] = [Tape(book), 0]
        entry[1] += 1
        try:
            yield entry[0]
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self.open[key]

    def get(self, key):
        """
        A contract's open tape, or None.
        """
        entry = self.open.get(key)
        return entry[0] if entry else None

    def add(self, key, book):
        """
        A contract's new book, or None when it was dropped, for its tape when one is open.
        """
        entry = self.open.get(key)
        if entry:
            entry[0].add(book)
