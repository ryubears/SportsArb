"""
Tests for the shared stream loop, driven by a scripted fake connection.
"""

import asyncio
import pytest
from api import bookstream


class FakeConnection:
    """
    A websocket stand in. It hands out scripted messages, records what was
    sent, and raises the scripted exception when the messages run out.
    """

    def __init__(self, messages, then):
        self.messages = list(messages)
        self.then = then
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, frame):
        self.sent.append(frame)

    async def recv(self):
        if self.messages:
            await asyncio.sleep(0)
            return self.messages.pop(0)
        if self.then == "hang":
            await asyncio.sleep(3600)
        raise self.then


class ParkedConnection:
    """
    Returned once the script runs out. Entering it never completes, which parks the loop.
    """

    async def __aenter__(self):
        await asyncio.sleep(3600)

    async def __aexit__(self, *args):
        return False


class ScriptedStream(bookstream.BookStream):
    """
    A venue with a scripted connection per attempt and trivial frames.
    """

    name = "scripted"
    stale_seconds = 0.05

    def __init__(self, contract_ids, connections, on_book=lambda *a: None, log=None):
        self.logs = []
        self.gaps = []
        super().__init__(contract_ids, on_book, self.logs.append, on_gap=lambda start, end: self.gaps.append((start, end)))
        self.connections = list(connections)
        self.used = []
        self.resets = 0
        self.handled = []

    def connect(self):
        if not self.connections:
            return ParkedConnection()
        conn = self.connections.pop(0)
        self.used.append(conn)
        return conn

    async def subscribe(self, ws):
        await ws.send(("subscribe", sorted(self.wanted)))

    async def send_command(self, ws, action, contract_ids):
        await ws.send((action, contract_ids))

    def handle(self, raw):
        if raw == "keepalive":
            return False
        if raw == "gap":
            raise bookstream.Reconnect("skipped a message")
        self.books[raw] = True
        self.handled.append(raw)
        return True

    def reset(self):
        self.resets += 1


def run_until_connections_used(stream):
    async def scenario():
        task = asyncio.create_task(stream.run())
        while stream.connections:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())


def test_loop_subscribes_and_reconnects_on_silence_gap_and_drop(monkeypatch):
    monkeypatch.setattr(bookstream, "RECONNECT_SECONDS", (0,))
    first = FakeConnection(["a"], then="hang")                          # Goes silent after one message.
    second = FakeConnection(["b", "gap"], then="hang")                  # Asks for a reconnect.
    third = FakeConnection(["c"], then=ConnectionResetError("peer reset"))   # Drops.
    fourth = FakeConnection([], then="hang")
    stream = ScriptedStream(["x", "y"], [first, second, third, fourth])
    run_until_connections_used(stream)
    assert [c.sent[0] for c in stream.used] == [("subscribe", ["x", "y"])] * 4
    # The final connection goes stale too before the harness stops, so only the scripted three are compared.
    assert stream.logs[:3] == ["scripted stream silent for 0.05s, reconnecting",
                           "scripted stream skipped a message, reconnecting",
                           "scripted stream dropped (ConnectionResetError: peer reset), reconnecting"]
    assert stream.resets >= 4
    assert stream.books == {}                                            # Cleared before the last connection.
    assert len(stream.gaps) == 3 and all(start <= end for start, end in stream.gaps)   # One gap per failure, closed on resubscribe.
    assert stream.num_failures == 1                                          # Reset by each resubscribe, then the last connection stalled.


def test_reconnect_pause_is_immediate_first_and_backs_off_after():
    assert [bookstream.reconnect_pause(n) for n in (1, 2, 3, 4, 5, 9)] == [0, 1, 3, 10, 10, 10]


class RefusingConnection:
    """
    A connection whose handshake is rejected, as a venue does when it refuses a client.
    """

    async def __aenter__(self):
        raise ValueError("server rejected WebSocket connection: HTTP 403")

    async def __aexit__(self, *args):
        return False


def test_loop_survives_a_rejected_handshake(monkeypatch):
    monkeypatch.setattr(bookstream, "RECONNECT_SECONDS", (0,))
    stream = ScriptedStream(["x"], [RefusingConnection(), FakeConnection(["a"], then="hang")])
    run_until_connections_used(stream)
    first, *trace = stream.logs[0].splitlines()
    assert first == "scripted stream failed (ValueError: server rejected WebSocket connection: HTTP 403), reconnecting"
    assert trace[0] == "    Traceback (most recent call last):" and trace[-1] == "    ValueError: server rejected WebSocket connection: HTTP 403"
    assert stream.handled == ["a"]                                       # The next connection was used normally.


def test_keepalive_replies_do_not_reset_the_stale_clock(monkeypatch):
    monkeypatch.setattr(bookstream, "RECONNECT_SECONDS", (0,))
    chatty = FakeConnection(["keepalive"] * 50, then="hang")
    stream = ScriptedStream(["x"], [chatty, FakeConnection([], then="hang")])
    run_until_connections_used(stream)
    assert stream.logs[0].startswith("scripted stream silent")


def test_changes_made_while_running_are_sent_and_pending_ones_dropped_on_connect(monkeypatch):
    monkeypatch.setattr(bookstream, "RECONNECT_SECONDS", (0,))
    conn = FakeConnection([], then="hang")
    stream = ScriptedStream(["x"], [conn])
    stream.add(["y"])       # Queued before the first connection, so it must not be sent as a command.

    async def scenario():
        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.02)
        stream.add(["z"])
        stream.remove(["x"])
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
    assert conn.sent == [("subscribe", ["x", "y"]), ("add", ["z"]), ("remove", ["x"])]
    assert stream.wanted == {"y", "z"}


def test_run_waits_until_something_is_wanted():
    stream = ScriptedStream([], [FakeConnection([], then="hang")])

    async def scenario():
        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.05)
        assert stream.used == []
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
