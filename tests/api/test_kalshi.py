"""
Tests for the Kalshi client's frames and book handling that need no network.
"""

import pytest
from api import bookstream, kalshi


def test_close_time_takes_the_earlier_of_close_and_expected_expiration():
    assert kalshi.close_time({"close_time": "2029-02-13T23:30:00Z", "expected_expiration_time": "2027-02-14T23:30:00Z"}) == "2027-02-14T23:30:00+00:00"
    assert kalshi.close_time({"close_time": "2026-09-24T00:15:00Z"}) == "2026-09-24T00:15:00+00:00"
    assert kalshi.close_time({}) is None


def test_update_frame():
    frame = kalshi.update_frame(7, 3, ["A", "B"], "add_markets")
    assert frame == {"id": 7, "cmd": "update_subscription", "params": {"sids": [3], "market_tickers": ["A", "B"], "action": "add_markets"}}


def test_stream_restates_no_side_as_yes_asks_and_tracks_sid():
    seen = []
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks: seen.append((ticker, bids, asks)))
    stream.reset()
    stream.handle('{"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 4}}')
    assert stream.sid == 4 and stream.subscribed.is_set()
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.48", "10"]], "no_dollars_fp": [["0.50", "5"]]}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.50", "delta_fp": "-5", "side": "no"}})
    assert seen[0] == ("T", [[0.48, 10.0]], [[0.5, 5.0]])
    assert seen[1] == ("T", [[0.48, 10.0]], [])


def test_stream_asks_to_reconnect_on_a_sequence_gap():
    stream = kalshi.KalshiBookStream(["T"], lambda *args: None)
    stream.reset()
    stream.handle('{"type": "ok", "seq": 1, "msg": {}}')
    stream.handle('{"type": "ok", "seq": 2, "msg": {}}')
    with pytest.raises(bookstream.Reconnect):
        stream.handle('{"type": "ok", "seq": 4, "msg": {}}')
