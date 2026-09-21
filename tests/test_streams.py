"""
Tests for the venue stream frames and book handling that need no network.
"""

import asyncio
import json
import pytest
from api import bookstream, kalshi, polymarket


class FakeSocket:
    """
    Collects the frames a stream sends.
    """

    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


def test_polymarket_subscribe_opens_with_one_chunk_and_queues_the_rest(monkeypatch):
    monkeypatch.setattr(polymarket, "WS_CHUNK", 2)
    stream = polymarket.PolymarketBookStream(["a", "b", "c", "d", "e"], lambda *args: None)
    stream.reset()
    ws = FakeSocket()
    asyncio.run(stream.subscribe(ws))
    assert ws.frames == [{"assets_ids": ["a", "b"], "type": "market"}]
    assert stream.pending == {"a", "b"}
    assert stream.commands.get_nowait() == ("add", ["c", "d", "e"])


def test_polymarket_adds_wait_for_the_last_chunks_snapshots(monkeypatch):
    monkeypatch.setattr(polymarket, "WS_CHUNK", 2)
    monkeypatch.setattr(polymarket, "WS_CHUNK_SECONDS", 0.3)
    stream = polymarket.PolymarketBookStream(["a", "b", "c", "d", "e"], lambda *args: None)
    stream.reset()
    stream.pending = {"a", "b"}
    ws = FakeSocket()

    async def scenario():
        sender = asyncio.create_task(stream.send_command(ws, "add", ["c", "d", "e"]))
        await asyncio.sleep(0.15)
        sent_before = len(ws.frames)
        stream.books["a"] = stream.books["b"] = {}     # The first chunk's snapshots land.
        await asyncio.sleep(0.15)
        sent_after = len(ws.frames)
        await sender
        return sent_before, sent_after

    sent_before, sent_after = asyncio.run(scenario())
    assert sent_before == 0 and sent_after == 1              # Waited for snapshots, then sent the next chunk.
    assert ws.frames[0] == {"assets_ids": ["c", "d"], "operation": "subscribe"}
    assert ws.frames[1] == {"assets_ids": ["e"], "operation": "subscribe"}    # Sent once the wait timed out.
    assert stream.pending == {"e"}


def test_polymarket_removes_are_sent_at_once(monkeypatch):
    monkeypatch.setattr(polymarket, "WS_CHUNK", 2)
    stream = polymarket.PolymarketBookStream(["a"], lambda *args: None)
    stream.reset()
    ws = FakeSocket()
    asyncio.run(stream.send_command(ws, "remove", ["x", "y", "z"]))
    assert ws.frames == [{"assets_ids": ["x", "y"], "operation": "unsubscribe"}, {"assets_ids": ["z"], "operation": "unsubscribe"}]


def test_polymarket_stream_queues_only_real_changes():
    stream = polymarket.PolymarketBookStream(["a", "b"], lambda *args: None)
    stream.add(["b", "c"])
    stream.remove(["a", "zzz"])
    assert stream.wanted == {"b", "c"}
    assert stream.commands.get_nowait() == ("add", ["c"])
    assert stream.commands.get_nowait() == ("remove", ["a"])
    assert stream.commands.empty()


def test_polymarket_stream_applies_snapshot_and_change():
    seen = []
    stream = polymarket.PolymarketBookStream(["t"], lambda token, bids, asks: seen.append((token, bids, asks)))
    stream.apply({"event_type": "book", "asset_id": "t", "bids": [{"price": "0.48", "size": "10"}], "asks": [{"price": "0.50", "size": "5"}]})
    stream.apply({"event_type": "price_change", "price_changes": [{"asset_id": "t", "price": "0.49", "size": "7", "side": "BUY"},
                                                                  {"asset_id": "other", "price": "0.1", "size": "1", "side": "BUY"}]})
    assert seen[0] == ("t", [[0.48, 10.0]], [[0.5, 5.0]])
    assert seen[1] == ("t", [[0.49, 7.0], [0.48, 10.0]], [[0.5, 5.0]])
    assert len(seen) == 2


def test_polymarket_pong_does_not_count_as_data():
    stream = polymarket.PolymarketBookStream(["t"], lambda *args: None)
    assert stream.handle("PONG") is False
    assert stream.handle('{"event_type": "price_change", "price_changes": []}') is True


def test_kalshi_close_time_takes_the_earlier_of_close_and_expected_expiration():
    assert kalshi.close_time({"close_time": "2029-02-13T23:30:00Z", "expected_expiration_time": "2027-02-14T23:30:00Z"}) == "2027-02-14T23:30:00+00:00"
    assert kalshi.close_time({"close_time": "2026-09-24T00:15:00Z"}) == "2026-09-24T00:15:00+00:00"
    assert kalshi.close_time({}) is None


def test_kalshi_update_frame():
    frame = kalshi.update_frame(7, 3, ["A", "B"], "add_markets")
    assert frame == {"id": 7, "cmd": "update_subscription", "params": {"sids": [3], "market_tickers": ["A", "B"], "action": "add_markets"}}


def test_kalshi_stream_restates_no_side_as_yes_asks_and_tracks_sid():
    seen = []
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks: seen.append((ticker, bids, asks)))
    stream.reset()
    stream.handle('{"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 4}}')
    assert stream.sid == 4 and stream.subscribed.is_set()
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.48", "10"]], "no_dollars_fp": [["0.50", "5"]]}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.50", "delta_fp": "-5", "side": "no"}})
    assert seen[0] == ("T", [[0.48, 10.0]], [[0.5, 5.0]])
    assert seen[1] == ("T", [[0.48, 10.0]], [])


def test_kalshi_stream_asks_to_reconnect_on_a_sequence_gap():
    stream = kalshi.KalshiBookStream(["T"], lambda *args: None)
    stream.reset()
    stream.handle('{"type": "ok", "seq": 1, "msg": {}}')
    stream.handle('{"type": "ok", "seq": 2, "msg": {}}')
    with pytest.raises(bookstream.Reconnect):
        stream.handle('{"type": "ok", "seq": 4, "msg": {}}')
