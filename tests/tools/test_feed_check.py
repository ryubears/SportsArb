"""
Tests for how the feed check reads the venues' messages and compares connections.
"""

from tools import feed_check


def test_a_polymarket_us_update_is_named_by_its_time_and_best_prices_after_the_snapshot_of_its_market():
    seen = set()
    book = {"marketData": {"marketSlug": "s", "transactTime": "2026-09-29T04:15:16.288755493Z",
                           "bids": [{"px": {"value": "0.30"}, "qty": "5"}, {"px": {"value": "0.31"}, "qty": "2"}], "offers": []}}
    lite = {"marketDataLite": {"marketSlug": "s", "bestBid": {"value": "0.3100"}, "bestAsk": None}}
    trade = {"trade": {"marketSlug": "t", "tradeTime": "2026-09-29T04:15:17Z", "price": {"value": "0.4"}}}
    assert feed_check.polymarket_update(book, seen) is None                     # The snapshot.
    assert feed_check.polymarket_update(book, seen) == ([("s", 1790655316.288755), ("best", "s", 0.31, None)], 1790655316.288755)
    assert feed_check.polymarket_update(lite, seen) == ([("best", "s", 0.31, None)], None)
    assert feed_check.polymarket_update(trade, seen) == ([("t", 1790655317.0)], 1790655317.0)    # A trade is no snapshot.
    assert feed_check.polymarket_update({"requestId": "check-0"}, seen) is None


def test_a_kalshi_delta_is_keyed_by_what_it_changed_and_other_messages_are_left_out():
    delta = {"type": "orderbook_delta", "seq": 7, "msg": {"market_ticker": "T", "ts_ms": 1790651367684, "side": "yes",
                                                          "price_dollars": "0.30", "delta_fp": "4.00"}}
    assert feed_check.kalshi_update(delta, set()) == ([("T", 1790651367684, "yes", "0.30", "4.00")], 1790651367.684)
    assert feed_check.kalshi_update({"type": "orderbook_snapshot", "msg": {"market_ticker": "T"}}, set()) is None


def test_connections_are_compared_on_the_updates_both_carried():
    first, second = feed_check.Listener("full book"), feed_check.Listener("lite")
    for key, sent, a, b in [("u1", 10.0, 10.080, 10.050), ("u2", 11.0, 11.090, 11.070), ("u3", 12.0, 12.1, None)]:
        first.count([key], sent, a, "{}")
        if b is not None:
            second.count([key], None, b, "{}")          # Such as the lite feed, which has no time.
    assert feed_check.compared(first, second) == "  lite got the same updates +25.0 ms sooner than full book (median of 2)"
    assert "median    90 ms" in first.line()
    assert "2 updates, none with the venue's time" in second.line()
