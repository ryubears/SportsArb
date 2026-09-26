"""
Tests for the kept connections the trading calls go over, with a stand in for the HTTPS connection.
"""

import json
import pytest
from api import http


class FakeResponse:
    def __init__(self, status, body, will_close=False):
        self.status, self.body, self.will_close = status, body, will_close

    def read(self):
        return self.body.encode()


class FakeConnection:
    """
    Answers each request with the next response in script, or raises it when it is an exception.
    """
    opened = []
    script = []

    def __init__(self, host, timeout=None):
        self.host, self.timeout, self.sock, self.closed, self.requests = host, timeout, None, False, []
        FakeConnection.opened.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        response = FakeConnection.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


@pytest.fixture
def connections(monkeypatch):
    FakeConnection.opened.clear()
    monkeypatch.setattr(http.http.client, "HTTPSConnection", FakeConnection)
    pool = http.Connections()
    monkeypatch.setattr(http, "CONNECTIONS", pool)
    return pool


def test_a_connection_is_kept_and_reused_while_fresh(connections):
    FakeConnection.script = [FakeResponse(200, '{"a": 1}'), FakeResponse(200, ""), FakeResponse(200, "{}")]
    assert http.send_json("POST", "https://api.example.com/v1/orders?x=1", {"K": "v"}, {"q": 5}) == {"a": 1}
    assert http.send_json("GET", "https://api.example.com/v1/balance") == {}
    assert len(FakeConnection.opened) == 1
    method, path, body, headers = FakeConnection.opened[0].requests[0]
    assert (method, path, json.loads(body), headers["K"], headers["Content-Type"]) == ("POST", "/v1/orders?x=1", {"q": 5}, "v", "application/json")
    connections.idle_seconds = -1           # Once idle too long, the kept connection is closed and a new one opened.
    http.send_json("GET", "https://api.example.com/v1/balance")
    assert len(FakeConnection.opened) == 2 and FakeConnection.opened[0].closed


def test_an_error_status_raises_with_the_body_and_a_failed_connection_is_dropped(connections):
    FakeConnection.script = [FakeResponse(409, '{"error": "insufficient"}'), ConnectionResetError("reset")]
    with pytest.raises(http.RequestFailed) as failed:
        http.send_json("POST", "https://api.example.com/v1/orders", body={})
    assert (failed.value.status, failed.value.body) == (409, '{"error": "insufficient"}')
    with pytest.raises(ConnectionResetError):
        http.send_json("POST", "https://api.example.com/v1/orders", body={})     # Never retried.
    assert len(FakeConnection.opened) == 1 and FakeConnection.opened[0].closed and connections.idle["api.example.com"] == []
