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


def test_balance_is_read_in_dollars_from_a_call_signed_on_the_full_path(monkeypatch):
    calls = fake_api(monkeypatch, {("GET", "/portfolio/balance"): {"balance": 123456, "portfolio_value": 0, "updated_ts": 0}})
    assert kalshi.balance() == 1234.56
    assert calls == [("GET", "/portfolio/balance", "GET /trade-api/v2/portfolio/balance", None)]


def test_an_order_is_an_immediate_or_cancel_limit_on_the_side_traded_and_a_sale_only_reduces():
    assert kalshi.order_body("T", "buy", "no", 5, 0.47, "c1") == {
        "ticker": "T", "client_order_id": "c1", "action": "buy", "side": "no", "count": 5, "type": "limit",
        "time_in_force": "immediate_or_cancel", "no_price_dollars": "0.4700"}
    assert kalshi.order_body("T", "sell", "yes", 3, 0.44, "c2")["reduce_only"] is True


def test_an_order_is_costed_from_its_fills_and_its_fees_from_the_order(monkeypatch):
    order = {"order_id": "o1", "status": "canceled", "fill_count": 4, "taker_fees_dollars": "0.0700", "taker_fill_cost_dollars": "1.8600"}
    fills = [{"order_id": "o1", "side": "no", "action": "buy", "count": 3, "no_price_fixed": "0.4600", "yes_price_fixed": "0.5400"},
             {"order_id": "o1", "side": "no", "action": "buy", "count": 1, "no_price_fixed": "0.4700", "yes_price_fixed": "0.5300"}]
    calls = fake_api(monkeypatch, {("POST", "/portfolio/orders"): {"order": order}, ("GET", "/portfolio/fills"): {"fills": fills}})
    answer = kalshi.place_order("T", "buy", "no", 5, 0.47, "c1")
    assert (answer.order_id, answer.status, answer.filled, answer.fees) == ("o1", "partial", 4, 0.07)
    assert answer.dollars == pytest.approx(3 * 0.46 + 0.47 + 0.07)
    assert calls[1][:2] == ("GET", "/portfolio/fills?order_id=o1") and calls[1][2] == "GET /trade-api/v2/portfolio/fills"


def test_a_sale_nets_the_fee_and_falls_back_to_its_limit_when_the_fills_are_not_visible_yet(monkeypatch):
    order = {"order_id": "o2", "status": "executed", "fill_count": 3, "taker_fees_dollars": "0.0600"}
    fake_api(monkeypatch, {("POST", "/portfolio/orders"): {"order": order}, ("GET", "/portfolio/fills"): {"fills": []}})
    answer = kalshi.place_order("T", "sell", "yes", 3, 0.44, "c2")
    assert (answer.status, answer.filled) == ("filled", 3) and answer.dollars == pytest.approx(3 * 0.44 - 0.06)


def test_an_order_that_did_not_fill_costs_nothing_and_needs_no_fills(monkeypatch):
    calls = fake_api(monkeypatch, {("POST", "/portfolio/orders"): {"order": {"order_id": "o3", "status": "canceled", "fill_count": 0}}})
    assert kalshi.place_order("T", "buy", "yes", 5, 0.45, "c3")[:5] == ("o3", "unfilled", 0, 0.0, 0.0)
    assert len(calls) == 1


@pytest.mark.parametrize("error, status", [
    (RequestFailed(400, '{"error": {"code": "insufficient_balance"}}'), "rejected"),     # Refused, nothing traded.
    (RequestFailed(503, "unavailable"), "error"),                                         # Failed on its side, it may have traded.
    (TimeoutError("timed out"), "error"),
])
def test_a_refused_order_is_told_apart_from_one_whose_fate_is_unknown(monkeypatch, error, status):
    fake_api(monkeypatch, {("POST", "/portfolio/orders"): error})
    answer = kalshi.place_order("T", "buy", "yes", 5, 0.45, "c4")
    assert (answer.status, answer.filled, answer.order_id) == (status, 0, None)
    assert answer.note
