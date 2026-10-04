"""
Tests for what our live orders took from the books, which paper gives back.
"""

import asyncio
from common.timeutil import at_seconds, epoch
from db.models import Book, Leg
from engine.components.market.tape import Tape
from engine.components.trading.footprints import Footprints
from trade_setup import NOW

T = epoch(NOW)
YES_PM = Leg("yes", "polymarket_us", "pm", "yes")       # Holds yes through the Polymarket US contract, bought at its asks.


def pm(asks, made, bids=()):
    """
    A book of the Polymarket US contract the venue made at made, seconds after NOW.
    """
    return Book("polymarket_us", "pm", at_seconds(T + made), [list(b) for b in bids], [list(a) for a in asks], T + made)


def answered(footprints, leg, limit, at, filled, selling=False):
    """
    A live order on leg sent at NOW that the venue handled at at, seconds after NOW, filling filled.
    """
    footprint = footprints.sent(leg, selling, limit)
    footprints.answer(footprint, "polymarket_us", {"executions": [{"transactTime": at_seconds(T + at)}]}, filled)
    return footprint


def test_what_a_live_order_took_is_given_back_until_the_level_falls_below_what_it_left():
    footprints = Footprints(clock=lambda: T)
    tape = Tape(pm([[0.45, 3], [0.46, 10]], -1))
    answered(footprints, YES_PM, 0.46, 0.060, 5)        # Took the 3 at 0.45 and 2 of the 10 at 0.46.
    after = pm([[0.46, 8]], 0.060)
    tape.add(after)
    assert footprints.give_back(YES_PM.key, after, tape).asks == [[0.45, 3], [0.46, 10]]
    assert footprints.give_back(YES_PM.key, tape.at(0.059 + T), tape).asks == [[0.45, 3], [0.46, 10]]   # Made before it arrived.
    # Another taker then took 2 more at 0.46, so ours there would have gone too, while 0.45 is still the gone level ours left.
    assert footprints.give_back(YES_PM.key, pm([[0.46, 6]], 0.080), tape).asks == [[0.45, 3], [0.46, 6]]
    # A new order joined 0.46, which is no fall: ours is still owed there.
    assert footprints.give_back(YES_PM.key, pm([[0.46, 9]], 0.090), tape).asks == [[0.45, 3], [0.46, 11]]


def test_a_sale_gives_back_the_bids_it_sold_into_no_lower_than_its_floor():
    footprints = Footprints(clock=lambda: T)
    tape = Tape(pm([[0.50, 10]], -1, bids=[[0.44, 2], [0.43, 2], [0.40, 50]]))
    answered(footprints, YES_PM, 0.43, 0.030, 3, selling=True)          # Sold 2 at 0.44 and 1 at 0.43.
    gave = footprints.give_back(YES_PM.key, pm([[0.50, 10]], 0.030, bids=[[0.43, 1], [0.40, 50]]), tape)
    assert gave.bids == [[0.44, 2], [0.43, 2], [0.40, 50]] and gave.asks == [[0.50, 10]]


def test_nothing_is_given_back_without_a_tape_from_before_the_order_or_for_an_order_that_filled_nothing():
    footprints = Footprints(clock=lambda: T)
    answered(footprints, YES_PM, 0.46, 0.060, 5)
    late = Tape(pm([[0.46, 8]], 0.061))                                 # Started after the order arrived.
    assert footprints.give_back(YES_PM.key, pm([[0.46, 8]], 0.070), late).asks == [[0.46, 8]]
    other = Footprints(clock=lambda: T)
    answered(other, YES_PM, 0.46, 0.060, 0)
    tape = Tape(pm([[0.45, 3]], -1))
    assert other.give_back(YES_PM.key, pm([[0.46, 8]], 0.070), tape).asks == [[0.46, 8]]


def test_footprints_are_kept_ten_seconds_and_paper_waits_for_the_answers_of_orders_sent_before_its_own():
    now = [T]
    footprints = Footprints(clock=lambda: now[0])
    first = footprints.sent(YES_PM, False, 0.46)
    now[0] = T + 11
    second = footprints.sent(YES_PM, False, 0.46)
    assert footprints.by_key[YES_PM.key] == [second]

    async def scenario():
        asyncio.get_running_loop().call_later(0.01, footprints.answer, second, "polymarket_us", {}, 0)
        await footprints.known(YES_PM.key, T + 12)
        return second.answered.is_set()
    assert asyncio.run(scenario()) and first.arrived is None
