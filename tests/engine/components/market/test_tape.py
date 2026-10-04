"""
Tests for the tapes paper orders meet the venues' books on.
"""

import asyncio
import time
from db import database
from db.models import Book
from engine.components.market.record import Recorder
from engine.components.market.tape import Tape, Tapes
from engine.components.trading.paper import at_seconds

T = 1_790_000_000.0         # A moment, in seconds since 1970.
KEY = ("polymarket_us", "pm")


def book(ask, at, received):
    """
    A Polymarket US book asking ask, made at at by the venue's clock and reaching us at received by ours.
    """
    return Book("polymarket_us", "pm", at_seconds(received), [[round(ask - 0.01, 2), 100]], [[ask, 100]], at)


def asks(found):
    return found.asks[0][0] if found else None


def test_a_tape_finds_the_book_the_venue_had_at_a_moment_and_the_one_we_had_seen():
    tape = Tape(book(0.45, T - 0.08, T))
    tape.add(book(0.46, T + 0.03, T + 0.11))
    tape.add(book(0.47, T + 0.07, T + 0.16))
    tape.add(book(0.48, None, T + 0.20))           # A new connection's first book, which the venue gives no time for.
    assert [asks(tape.at(T + s)) for s in (0, 0.029, 0.03, 0.05, 0.07, 0.2, 1)] == [0.45, 0.45, 0.46, 0.46, 0.47, 0.48, 0.48]
    assert [asks(tape.seen(T + s)) for s in (-1, 0.1, 0.11, 0.159, 0.2)] == [0.45, 0.45, 0.46, 0.46, 0.48]


def test_a_dropped_book_is_none_until_the_next():
    tape = Tape(book(0.45, T - 0.08, T))
    tape.add(None)
    assert tape.at(T + 1) is None
    tape.add(book(0.46, None, T + 0.5))           # The new connection's first book.
    assert tape.at(T + 0.4) is None and asks(tape.at(T + 1)) == 0.46


def test_a_tape_waits_until_a_book_made_later_reaches_us_or_the_time_is_up():
    async def scenario():
        tape = Tape(book(0.45, T, T))
        assert not tape.complete(T + 0.05)
        waiting = asyncio.create_task(tape.wait(T + 0.05, 5))
        await asyncio.sleep(0)
        tape.add(book(0.46, T + 0.04, T + 0.12))            # Made before the moment: there may be more.
        await asyncio.sleep(0)
        assert not waiting.done()
        tape.add(book(0.47, T + 0.06, T + 0.14))            # Made after it: every book up to it is in.
        started = time.monotonic()
        await waiting
        assert time.monotonic() - started < 1 and tape.complete(T + 0.05)
        started = time.monotonic()
        await tape.wait(T + 1, 0.02)                         # No later book comes.
        assert 0.015 < time.monotonic() - started < 1
    asyncio.run(scenario())


def test_orders_on_a_contract_share_one_tape_kept_only_while_one_is_in_flight():
    tapes = Tapes()
    tapes.add(KEY, book(0.44, T - 1, T - 1))                # No tape is open, so it goes nowhere.
    with tapes.follow(KEY, book(0.45, T, T)) as first:
        with tapes.follow(KEY, book(0.46, T + 1, T + 1)) as second:
            assert second is first and tapes.get(KEY) is first
            tapes.add(KEY, book(0.47, T + 2, T + 2))
            tapes.add(("kalshi", "k"), book(0.10, T + 2, T + 2))
        assert tapes.get(KEY) is first                       # The first order is still in flight.
    assert tapes.get(KEY) is None and tapes.open == {}
    assert [asks(b) for _, b in first.books] == [0.45, 0.47]


def test_the_recorder_tapes_each_book_of_a_followed_contract_and_a_dropped_one_as_none(tmp_path):
    tapes = Tapes()
    recorder = Recorder(database.connect(tmp_path / "t.sqlite"), tapes=tapes)
    with tapes.follow(KEY, None) as tape:
        recorder.on_book("polymarket_us", "pm", [[0.44, 100]], [[0.45, 100]], ts=at_seconds(T), sent=T - 0.08)
        recorder.on_book("kalshi", "k", [[0.53, 100]], [[0.54, 100]], ts=at_seconds(T), sent=T - 0.01)
        recorder.forget("polymarket_us", ["pm"])
        recorder.forget("polymarket_us", ["pm"])             # Already gone, so nothing more.
    assert [(b and b.at, b and asks(b)) for _, b in tape.books] == [(None, None), (T - 0.08, 0.45), (None, None)]
