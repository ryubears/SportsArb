"""
Tests for the Polymarket US client's book handling that need no network.
"""

import asyncio
from api import polymarket_us
from fake_socket import Socket


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
    asyncio.run(stream.subscribe(Socket()))
    assert stream.handle('{"requestId": "md-1", "error": "unknown market"}') is False
    assert stream.handle('{"requestId": "md-11", "error": "max subscriptions per connection reached"}') is False     # Never sent.
    assert logs == ["polymarket_us stream error unknown market on md-1",
                    "polymarket_us stream error max subscriptions per connection reached on md-11"]
    assert stream.wanted == {"s"}


def test_room_counts_the_subscription_requests_left_rather_than_the_slugs():
    stream = polymarket_us.PolymarketUSBookStream([f"s{i:03}" for i in range(950)], lambda *args: None)
    assert polymarket_us.PolymarketUSBookStream.capacity == 1000
    assert stream.room() == 50                          # Before it subscribes, only the slug capacity bounds it.
    ws = Socket()
    asyncio.run(stream.subscribe(ws))
    assert len(ws.sent) == 10 and stream.room() == 0    # Ten requests, the last carrying 50 slugs, leave none for an add.


def test_every_add_spends_a_request_however_few_slugs_it_brings():
    stream = polymarket_us.PolymarketUSBookStream([f"s{i:03}" for i in range(420)], lambda *args: None)
    asyncio.run(stream.subscribe(Socket()))
    assert stream.room() == 500                         # Five requests spent, five left.
    for refresh in range(5):
        stream.add([f"r{refresh}-{i}" for i in range(28)])
    assert len(stream.wanted) == 560 and stream.room() == 0     # Counting slugs, it would take 440 more.
    stream.remove(["s000"])
    assert stream.room() == 0                           # With no unsubscribe, a removal gives no request back.


def test_a_refused_subscription_fills_the_connection_and_hands_back_its_slugs():
    logs, handed = [], []
    stream = polymarket_us.PolymarketUSBookStream(["a", "b"], lambda *args: None, log=logs.append)
    stream.on_refused = handed.append
    ws = Socket()
    asyncio.run(stream.subscribe(ws))
    stream.add(["c", "d", "e"])
    asyncio.run(stream.send_command(ws, *stream.commands.get_nowait()))
    stream.remove(["e"])
    assert [frame["subscribe"]["requestId"] for frame in ws.sent] == ["md-1", "md-2"]
    assert stream.handle('{"requestId": "md-2", "error": "max subscriptions per connection reached"}') is False
    assert handed == [["c", "d"]]                       # e was removed since, so it needs no other connection.
    assert stream.wanted == {"a", "b"} and stream.room() == 0
    assert logs == ["polymarket_us refused md-2 as one subscription too many, moving its 2 contracts to another connection"]


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


def test_game_states_come_from_one_call_asking_for_each_events_moneyline_only(monkeypatch):
    calls = []

    def get_json(url, params):
        calls.append((url, params))
        return {"events": [{"slug": "cfb-minnst-wash-2026-09-26", "period": "VFT", "live": False, "ended": True,
                            "finishedTimestamp": "2026-09-27T06:27:42Z"},
                           {"slug": "nfl-phi-chi-2026-09-28", "period": "Q2", "live": True},
                           {"slug": "nfl-car-atl-2026-10-04", "period": "NS"}]}
    monkeypatch.setattr(polymarket_us, "get_json", get_json)
    states = polymarket_us.game_states(["nfl-phi-chi-2026-09-28", "cfb-minnst-wash-2026-09-26", "nfl-car-atl-2026-10-04"])
    assert states == {"cfb-minnst-wash-2026-09-26": {"live": False, "ended": True, "finished": "2026-09-27T06:27:42+00:00"},
                      "nfl-phi-chi-2026-09-28": {"live": True, "ended": False, "finished": None},
                      "nfl-car-atl-2026-10-04": {"live": False, "ended": False, "finished": None}}
    [(url, params)] = calls
    assert url.endswith("/events") and [v for k, v in params if k == "slug"] == sorted(states)
    assert ("marketTypes", "moneyline") in params and ("limit", 3) in params


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


def test_the_latency_stopgap_leaves_an_order_unfilled_rather_than_refused(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_REJECTED", "text": "Global Rate Limit Exceeded"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p5", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c5")
    assert (answer.status, answer.filled, answer.note) == ("unfilled", 0, "latency stopgap: Global Rate Limit Exceeded")
    fake_api(monkeypatch, {("POST", "/orders"): RequestFailed(429, '{"status": 429, "message": "Global Rate Limit Exceeded"}')})
    assert polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c6").status == "unfilled"


def test_an_order_waits_as_long_as_the_stopgap_for_its_answer():
    assert polymarket_us.order_body("slug", "buy", "yes", 3, 0.44)["maxBlockTime"] == "5"


def test_an_order_turned_away_for_no_liquidity_is_unfilled_rather_than_refused(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "ORD_REJECT_REASON_NO_LIQUIDITY"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p7", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c7")
    assert (answer.status, answer.filled, answer.note) == ("unfilled", 0, "no liquidity: ORD_REJECT_REASON_NO_LIQUIDITY")


def test_an_answer_that_does_not_say_how_the_order_ended_is_followed_by_a_look_at_the_order(monkeypatch):
    partial = [{"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "1", "lastPx": {"value": "0.54"}}]
    order = {"id": "p8", "state": "ORDER_STATE_CANCELED", "cumQuantity": 3, "avgPx": {"value": "0.54", "currency": "USD"},
             "commissionNotionalTotalCollected": {"value": "0.05", "currency": "USD"}}
    calls = fake_api(monkeypatch, {("POST", "/orders"): {"id": "p8", "executions": partial}, ("GET", "/order/p8"): {"order": order}})
    answer = polymarket_us.place_order("slug", "buy", "no", 5, 0.47, "c8")
    # The order went on to fill 3 before the rest was canceled, bought as no at a yes price of 0.54.
    assert (answer.order_id, answer.status, answer.filled) == ("p8", "partial", 3)
    assert answer.dollars == pytest.approx(3 * 0.46 + 0.05) and answer.fees == pytest.approx(0.05)
    assert calls[1][:3] == ("GET", "/order/p8", "GET /v1/order/p8")


def test_an_order_that_has_still_not_ended_has_an_unknown_fate(monkeypatch):
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p9", "executions": []},
                           ("GET", "/order/p9"): {"order": {"id": "p9", "state": "ORDER_STATE_PENDING_NEW"}}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 5, 0.44, "c9")
    assert (answer.status, answer.order_id, answer.filled) == ("error", "p9", 0) and "still ORDER_STATE_PENDING_NEW" in answer.note
    fake_api(monkeypatch, {("POST", "/orders"): {"executions": []}})
    assert polymarket_us.place_order("slug", "buy", "yes", 5, 0.44, "c10").status == "error"      # Not even an order id to look up.
