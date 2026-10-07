"""
Tests for the venue connections behind the recorder.
"""

import asyncio
from db import database
from engine.components.market import record, streams


def test_streams_change_subscriptions_in_place(tmp_path, fake_stream):
    async def scenario():
        r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
        s = streams.Streams(r, {"polymarket_us": fake_stream, "kalshi": fake_stream})
        s.start("polymarket_us", ["a", "b"])
        s.start("kalshi", ["k1"])
        await asyncio.sleep(0)
        summary = s.update({"polymarket_us": ["a", "c"], "kalshi": ["k1"]})
        (pm,) = s.feeds["polymarket_us"].streams
        pm.on_book("b", [[0.5, 1]], [[0.6, 1]])    # A late update for the removed contract.
        pm.on_book("a", [[0.5, 1]], [[0.6, 1]])
        await s.stop_all()
        return summary, pm, r.books
    summary, pm, latest = asyncio.run(scenario())
    assert summary == "polymarket_us +1 -1"
    assert len(fake_stream.instances) == 2                 # No connection was replaced.
    assert all(s.on_gap is not None for s in fake_stream.instances)
    assert (pm.added, pm.removed, pm.wanted) == ([["c"]], [["b"]], {"a", "c"})
    assert ("polymarket_us", "b") not in latest
    assert ("polymarket_us", "a") in latest


def test_streams_open_more_connections_when_a_venue_has_a_capacity(tmp_path, fake_stream):
    class SmallStream(fake_stream):
        capacity = 2                                        # A venue that allows two contracts per connection.

    async def scenario():
        r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
        s = streams.Streams(r, {"polymarket_us": SmallStream, "kalshi": fake_stream})
        s.start("polymarket_us", ["a", "b", "c"])
        s.start("kalshi", ["k1", "k2", "k3"])
        await asyncio.sleep(0)
        first = [sorted(x.wanted) for x in s.feeds["polymarket_us"].streams]
        summary = s.update({"polymarket_us": ["b", "c", "d", "e", "f"], "kalshi": ["k1", "k2", "k3"]})
        after = [sorted(x.wanted) for x in s.feeds["polymarket_us"].streams]
        await s.stop_all()
        return first, summary, after, len(s.feeds["kalshi"].streams)
    first, summary, after, kalshi_connections = asyncio.run(scenario())
    assert first == [["a", "b"], ["c"]]                  # Split at the capacity.
    assert summary == "polymarket_us +3 -1"
    assert after == [["b", "d"], ["c", "e"], ["f"]]      # Room on the open connections is used first, then a third opens.
    assert kalshi_connections == 1                        # No capacity, one connection whatever the size.


def test_a_drop_on_one_connection_leaves_the_books_of_the_others(tmp_path, fake_stream):
    class SmallStream(fake_stream):
        capacity = 2

    async def scenario():
        r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
        s = streams.Streams(r, {"polymarket_us": SmallStream, "kalshi": fake_stream})
        s.start("polymarket_us", ["a", "b", "c"])
        first, second = s.feeds["polymarket_us"].streams
        for stream, contract_id in ((first, "a"), (first, "b"), (second, "c")):
            stream.on_book(contract_id, [[0.5, 1]], [[0.6, 1]])
        second.on_gap("2026-09-27T17:00:00+00:00", "2026-09-27T17:00:05+00:00", sorted(second.wanted))
        await s.stop_all()
        return r.books
    latest = asyncio.run(scenario())
    assert sorted(latest) == [("polymarket_us", "a"), ("polymarket_us", "b")]     # Only c waits for its connection to send it again.


def test_what_a_venue_says_of_its_markets_reaches_the_recorder_unless_the_contract_was_removed(tmp_path, fake_stream):
    async def scenario():
        r = record.Recorder(database.connect(tmp_path / "test.sqlite"), log=lambda line: None)
        s = streams.Streams(r, {"polymarket_us": fake_stream, "kalshi": fake_stream})
        s.start("polymarket_us", ["a", "b"])
        s.start("kalshi", ["k1"])
        await asyncio.sleep(0)
        s.update({"polymarket_us": ["a"], "kalshi": ["k1"]})
        (pm,) = s.feeds["polymarket_us"].streams
        pm.on_state("a", "suspended")
        pm.on_state("b", "expired")                 # A late word on the removed contract.
        await s.stop_all()
        return r.states
    assert asyncio.run(scenario()) == {("polymarket_us", "a"): "suspended"}
