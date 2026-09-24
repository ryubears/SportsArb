"""
Tests for the Polymarket US client's book handling that need no network.
"""

from api import polymarket_us


def test_stream_replaces_the_book_from_each_message():
    seen = []
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda slug, bids, asks: seen.append((slug, bids, asks)))
    stream.reset()
    assert stream.handle('{"requestId": "md-1", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA"}') is False
    stream.handle('{"marketData": {"marketSlug": "s", "bids": [{"px": {"value": "0.30"}, "qty": "5"}, {"px": {"value": "0.31"}, "qty": "2"}], "offers": [{"px": {"value": "0.33"}, "qty": "1"}]}}')
    stream.handle('{"marketData": {"marketSlug": "other", "bids": [], "offers": []}}')
    assert seen == [("s", [[0.31, 2.0], [0.30, 5.0]], [[0.33, 1.0]])]


def test_levels_drop_empty_sizes_and_sort_best_first():
    entries = [{"px": {"value": "0.40"}, "qty": "0"}, {"px": {"value": "0.42"}, "qty": "3"}, {"px": {"value": "0.41"}, "qty": "1"}]
    assert polymarket_us.levels(entries, reverse=True) == [[0.42, 3.0], [0.41, 1.0]]
    assert polymarket_us.levels(entries, reverse=False) == [[0.41, 1.0], [0.42, 3.0]]
    assert polymarket_us.levels(None, reverse=True) == []


def test_error_frames_are_logged_and_do_not_count_as_data():
    logs = []
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda *args: None, logs.append)
    stream.reset()
    assert stream.handle('{"requestId": "md-11", "error": "max subscriptions per connection reached"}') is False
    assert logs == ["polymarket_us stream error max subscriptions per connection reached on md-11"]
    assert polymarket_us.PolymarketUSBookStream.capacity == 1000
