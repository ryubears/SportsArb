"""
Tests for the venue stream frames and book handling that need no network.
"""

import pytest
from api import bookstream, kalshi, polymarket, polymarket_us


def test_polymarket_subscribe_frames_split_at_the_snapshot_limit():
    tokens = [str(i) for i in range(1201)]
    frames = polymarket.subscribe_frames(tokens)
    assert [len(f["assets_ids"]) for f in frames] == [500, 500, 201]
    assert frames[0]["type"] == "market" and "operation" not in frames[0]
    assert all(f["operation"] == "subscribe" for f in frames[1:])
    assert polymarket.subscribe_frames([]) == []


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


def test_polymarket_pong_does_not_count_as_data():
    stream = polymarket.PolymarketBookStream(["t"], lambda *args: None)
    assert stream.handle("PONG") is False
    assert stream.handle('{"event_type": "price_change", "price_changes": []}') is True


def test_polymarket_us_stream_replaces_the_book_from_each_message():
    seen = []
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda slug, bids, asks: seen.append((slug, bids, asks)))
    stream.reset()
    assert stream.handle('{"requestId": "md-1", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA"}') is False
    stream.handle('{"marketData": {"marketSlug": "s", "bids": [{"px": {"value": "0.30"}, "qty": "5"}, {"px": {"value": "0.31"}, "qty": "2"}], "offers": [{"px": {"value": "0.33"}, "qty": "1"}]}}')
    stream.handle('{"marketData": {"marketSlug": "other", "bids": [], "offers": []}}')
    assert seen == [("s", [[0.31, 2.0], [0.30, 5.0]], [[0.33, 1.0]])]
