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
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda *args: None, log=logs.append)
    stream.reset()
    assert stream.handle('{"requestId": "md-11", "error": "max subscriptions per connection reached"}') is False
    assert logs == ["polymarket_us stream error max subscriptions per connection reached on md-11"]
    assert polymarket_us.PolymarketUSBookStream.capacity == 1000


# TRADING

import pytest
from api.http import RequestFailed


def fake_api(monkeypatch, answers):
    """
    Answer signed calls from a table of {(method, path): answer or exception}, recording each call.
    """
    calls = []

    def send_json(method, url, headers=None, body=None, timeout=10):
        path = url.removeprefix(polymarket_us.API)
        calls.append((method, path, headers["signed"], body))
        answer = answers[(method, path)]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(polymarket_us, "send_json", send_json)
    monkeypatch.setattr(polymarket_us, "signed_headers", lambda method, path: {"signed": f"{method} {path}"})
    return calls


def test_balance_is_the_buying_power_of_the_dollar_balance(monkeypatch):
    calls = fake_api(monkeypatch, {("GET", "/account/balances"): {"balances": [{"currentBalance": 1500.0, "buyingPower": 1234.5, "currency": "USD"}]}})
    assert polymarket_us.balance() == 1234.5
    assert calls[0][2] == "GET /v1/account/balances"


def test_an_order_prices_the_short_side_on_the_long_side():
    body = polymarket_us.order_body("slug", "buy", "no", 5, 0.47)
    assert body["intent"] == "ORDER_INTENT_BUY_SHORT" and body["price"] == {"value": "0.53", "currency": "USD"}
    assert (body["tif"], body["type"], body["synchronousExecution"], body["quantity"]) == (
        "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "ORDER_TYPE_LIMIT", True, 5)
    assert polymarket_us.order_body("slug", "sell", "yes", 3, 0.5)["price"]["value"] == "0.5"
    assert polymarket_us.order_body("slug", "sell", "yes", 3, 0.5)["intent"] == "ORDER_INTENT_SELL_LONG"


def test_an_order_is_costed_from_its_executions(monkeypatch):
    executions = [
        {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "3", "lastPx": {"value": "0.54", "currency": "USD"},
         "commissionNotionalCollected": {"value": "0.05", "currency": "USD"}},
        {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "1", "lastPx": {"value": "0.53", "currency": "USD"},
         "commissionNotionalCollected": {"value": "0.02", "currency": "USD"}},
        {"type": "EXECUTION_TYPE_CANCELED"}]
    calls = fake_api(monkeypatch, {("POST", "/orders"): {"id": "p1", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "no", 5, 0.47, "c1")
    # Short side fills at long prices 0.54 and 0.53 cost 0.46 and 0.47.
    assert (answer.order_id, answer.status, answer.filled, answer.fees) == ("p1", "partial", 4, pytest.approx(0.07))
    assert answer.dollars == pytest.approx(3 * 0.46 + 0.47 + 0.07)
    assert calls[0][2] == "POST /v1/orders"


def test_a_sale_nets_the_fee(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_FILL", "lastShares": "3", "lastPx": {"value": "0.44"}, "commissionNotionalCollected": {"value": "0.05"}}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p2", "executions": executions}})
    answer = polymarket_us.place_order("slug", "sell", "yes", 3, 0.44, "c2")
    assert (answer.status, answer.filled) == ("filled", 3) and answer.dollars == pytest.approx(3 * 0.44 - 0.05)


def test_a_rejected_order_says_why(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "insufficient buying power"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p3", "executions": executions}})
    assert polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c3")[1:6] == ("rejected", 0, 0.0, 0.0, "insufficient buying power")


@pytest.mark.parametrize("error, status", [(RequestFailed(400, "bad price"), "rejected"), (RequestFailed(502, "bad gateway"), "error"),
                                           (ConnectionResetError("reset"), "error")])
def test_a_refused_order_is_told_apart_from_one_whose_fate_is_unknown(monkeypatch, error, status):
    fake_api(monkeypatch, {("POST", "/orders"): error})
    assert polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c4").status == status
