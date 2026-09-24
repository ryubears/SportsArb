"""
Tests for the venue connections behind the recorder.
"""

import asyncio
from db import database
from live import record, streams


def test_streams_change_subscriptions_in_place(tmp_path, fake_stream):
    async def scenario():
        r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
        s = streams.Streams(r, {"polymarket_us": fake_stream, "kalshi": fake_stream})
        s.start("polymarket_us", ["a", "b"])
        s.start("kalshi", ["k1"])
        await asyncio.sleep(0)
        summary = s.update({"polymarket_us": ["a", "c"], "kalshi": ["k1"]})
        (pm,) = s.streams["polymarket_us"]
        pm.on_book("b", [[0.5, 1]], [[0.6, 1]])    # A late update for the removed contract.
        pm.on_book("a", [[0.5, 1]], [[0.6, 1]])
        await s.stop_all()
        return summary, pm, r.latest
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
        first = [sorted(x.wanted) for x in s.streams["polymarket_us"]]
        summary = s.update({"polymarket_us": ["b", "c", "d", "e", "f"], "kalshi": ["k1", "k2", "k3"]})
        after = [sorted(x.wanted) for x in s.streams["polymarket_us"]]
        await s.stop_all()
        return first, summary, after, len(s.streams["kalshi"])
    first, summary, after, kalshi_connections = asyncio.run(scenario())
    assert first == [["a", "b"], ["c"]]                  # Split at the capacity.
    assert summary == "polymarket_us +3 -1"
    assert after == [["b", "d"], ["c", "e"], ["f"]]      # Room on the open connections is used first, then a third opens.
    assert kalshi_connections == 1                        # No capacity, one connection whatever the size.
