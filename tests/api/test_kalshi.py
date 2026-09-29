"""
Tests for the Kalshi client's frames and book handling that need no network.
"""

import asyncio
import json
import pytest
import random
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
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks, sent: seen.append((ticker, bids, asks)))
    stream.reset()
    stream.handle('{"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 4}}')
    assert stream.sid == 4 and stream.subscribed.is_set()
    # With use_yes_price, a resting No order comes priced as the Yes ask it is.
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.28", "10"], ["0.30", "4"]],
                                                        "no_dollars_fp": [["0.35", "7"], ["0.32", "5"]]}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.32", "delta_fp": "-5", "side": "no"}})
    assert seen[0] == ("T", [[0.30, 4.0], [0.28, 10.0]], [[0.32, 5.0], [0.35, 7.0]])
    assert seen[1] == ("T", [[0.30, 4.0], [0.28, 10.0]], [[0.35, 7.0]])


def test_the_levels_passed_on_are_the_best_of_the_whole_book_after_every_delta():
    rng = random.Random(3)
    seen = []
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks, sent: seen.append((bids, asks)))
    stream.reset()
    stream.depth = 3
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.40", "5"], ["0.39", "0"]],
                                                        "no_dollars_fp": [["0.45", "5"]]}})
    for _ in range(3000):
        stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": f"{rng.randint(1, 99) / 100:.4f}",
                                                         "delta_fp": str(rng.randint(-20, 20)), "side": rng.choice(["yes", "no"])}})
        book = stream.books["T"]
        assert all(size > 0 for side in ("yes", "no") for size in book[side].values())
        assert seen[-1] == ([[p, s] for p, s in sorted(book["yes"].items(), reverse=True)][:3],
                            [[p, s] for p, s in sorted(book["no"].items())][:3])


def test_a_delta_carries_the_matching_engine_time_and_a_snapshot_none():
    sent = []
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks, at: sent.append(at))
    stream.reset()
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.28", "10"]], "no_dollars_fp": []}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.30", "delta_fp": "4", "side": "yes",
                                                     "ts_ms": 1790651367684}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.30", "delta_fp": "1", "side": "yes"}})
    assert sent == [None, 1790651367.684, None]


def test_stream_asks_to_reconnect_on_a_sequence_gap():
    stream = kalshi.KalshiBookStream(["T"], lambda *args: None)
    stream.reset()
    stream.handle('{"type": "ok", "seq": 1, "msg": {}}')
    stream.handle('{"type": "ok", "seq": 2, "msg": {}}')
    with pytest.raises(bookstream.Reconnect):
        stream.handle('{"type": "ok", "seq": 4, "msg": {}}')


def test_results_are_looked_up_in_batches_and_only_finalized_markets_count(monkeypatch):
    calls = []

    def fake_get_json(url, params=None, retries=3):
        calls.append(params["tickers"])
        markets = [{"ticker": t, "status": "finalized", "result": "yes", "settlement_ts": "2026-09-25T03:23:29.0724Z"} for t in params["tickers"].split(",")]
        markets[0]["status"] = "active"
        return {"markets": markets}

    monkeypatch.setattr(kalshi, "get_json", fake_get_json)
    monkeypatch.setattr(kalshi, "RESULTS_BATCH", 2)
    monkeypatch.setattr(kalshi.time, "sleep", lambda s: None)
    out = kalshi.results(["c", "a", "b", "a"])
    assert calls == ["a,b", "c"]                                         # Sorted, deduplicated, two per call.
    assert out == {"b": ("yes", "2026-09-25T03:23:29.072400+00:00")}    # 'a' and 'c' were first in their batch and still active.


# TRADING

from api.http import RequestFailed


def fake_api(monkeypatch, answers):
    """
    Answer signed calls from a table of {(method, path without query): answer or exception}, recording each call.
    """
    calls = []

    def send_json(method, url, headers=None, body=None, timeout=10):
        path = url.removeprefix(kalshi.BASE)
        calls.append((method, path, headers["signed"], body))
        answer = answers[(method, path.split("?")[0])]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(kalshi, "send_json", send_json)
    monkeypatch.setattr(kalshi, "signed_headers", lambda method, path: {"signed": f"{method} {path}"})
    return calls


def test_the_balance_and_each_shards_are_read_in_dollars_from_one_call_signed_on_the_full_path(monkeypatch):
    breakdown = [{"exchange_index": 0, "balance": "1000.0000"}, {"exchange_index": 3, "balance": "234.5600"}]
    calls = fake_api(monkeypatch, {("GET", "/portfolio/balance"): {"balance": 123456, "balance_dollars": "1234.5600", "portfolio_value": 0,
                                                                   "balance_breakdown": breakdown}})
    assert kalshi.balances() == (1234.56, {0: 1000.0, 3: 234.56})
    assert calls == [("GET", "/portfolio/balance", "GET /trade-api/v2/portfolio/balance", None)]
    fake_api(monkeypatch, {("GET", "/portfolio/balance"): {"balance": 123456}})       # Before the dollar field and the shards, cents.
    assert kalshi.balances() == (1234.56, {})


def test_the_book_stream_asks_for_yes_side_prices():
    sent = []

    class FakeSocket:
        async def send(self, text):
            sent.append(json.loads(text))
    stream = kalshi.KalshiBookStream(["B", "A"], lambda *args: None)
    asyncio.run(stream.subscribe(FakeSocket()))
    assert sent == [{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": ["A", "B"], "use_yes_price": True}}]


def test_an_order_is_quoted_on_the_yes_side_and_a_sale_only_reduces():
    assert kalshi.order_body("T", "buy", "no", 5, 0.47, "c1") == {
        "ticker": "T", "client_order_id": "c1", "side": "ask", "count": "5", "price": "0.5300",
        "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}     # Buying no at 0.47 is selling yes at 0.53.
    assert kalshi.order_body("T", "buy", "yes", 5, 0.45, "c1")[("side")] == "bid"
    sale = kalshi.order_body("T", "sell", "no", 3, 0.44, "c2")
    assert (sale["side"], sale["price"], sale["reduce_only"]) == ("bid", "0.5600", True)       # Selling no at 0.44 is buying yes at 0.56.
    assert kalshi.order_body("T", "sell", "yes", 3, 0.44, "c2")["side"] == "ask"


def test_an_order_is_costed_from_its_average_price_and_fee(monkeypatch):
    answer = {"order_id": "o1", "client_order_id": "c1", "fill_count": "4.00", "remaining_count": "0.00",
              "average_fill_price": "0.5350", "average_fee_paid": "0.0175", "ts_ms": 1}
    calls = fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): answer})
    placed = kalshi.place_order("T", "buy", "no", 5, 0.47, "c1")
    # Bought 4 no at a yes price of 0.535 on average, so 0.465 each, and 0.0175 a contract in fees.
    assert (placed.order_id, placed.status, placed.filled, placed.note) == ("o1", "partial", 4, None)
    assert placed.fees == pytest.approx(0.07) and placed.dollars == pytest.approx(4 * 0.465 + 0.07)
    assert calls[0][2] == "POST /trade-api/v2/portfolio/events/orders" and len(calls) == 1


def test_a_sale_nets_the_fee_and_a_fractional_fill_counts_its_whole_contracts(monkeypatch):
    answer = {"order_id": "o2", "fill_count": "3.50", "remaining_count": "0.00", "average_fill_price": "0.4400", "average_fee_paid": "0.0200"}
    fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): answer})
    placed = kalshi.place_order("T", "sell", "yes", 4, 0.44, "c2")
    assert (placed.status, placed.filled, placed.note) == ("partial", 3, "fractional fill of 3.5 contracts")
    assert placed.dollars == pytest.approx(3 * 0.44 - 3 * 0.02)


def test_an_order_that_did_not_fill_costs_nothing(monkeypatch):
    fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): {"order_id": "o3", "fill_count": "0.00", "remaining_count": "0.00", "ts_ms": 1}})
    assert kalshi.place_order("T", "buy", "yes", 5, 0.45, "c3")[:5] == ("o3", "unfilled", 0, 0.0, 0.0)


@pytest.mark.parametrize("error, status", [
    (RequestFailed(400, '{"code": "insufficient_balance", "message": "insufficient balance"}'), "rejected"),     # Refused, nothing traded.
    (RequestFailed(503, "unavailable"), "error"),                                                                   # Failed on its side, it may have traded.
    # The market's exchange shard lacked the cash, as Kalshi answered on 2026-09-29. Nothing traded, and no refusal to halt on.
    (RequestFailed(404, '{"error":{"code":"insufficient_shard_balance","message":"insufficient shard balance","details":'
                        '"Exchange user not found. For Predictions: reference documentation Exchange Sharding documentation."}}'), "unfilled"),
    (TimeoutError("timed out"), "error"),
])
def test_a_refused_order_is_told_apart_from_one_whose_fate_is_unknown(monkeypatch, error, status):
    fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): error})
    answer = kalshi.place_order("T", "buy", "yes", 5, 0.45, "c4")
    assert (answer.status, answer.filled, answer.order_id) == (status, 0, None)
    assert answer.note
