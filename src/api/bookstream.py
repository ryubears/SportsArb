"""
Shared logic for the venue book streams.

A book stream keeps one websocket connection carrying every wanted
contract, keeps a live order book per contract, and hands each change to
a callback. This base class holds what the venues have in common: the
wanted set, the queue of changes made while the connection runs, and the
connect, subscribe, read, and reconnect loop. A venue subclass supplies
how to connect, what to send to subscribe, how to turn a queued change
into a frame, how to apply one message, and any keepalive the feed needs.
"""

import asyncio
import time
import websockets
from common.timeutil import now_iso

# Pause before each attempt after a failure. The first retry is immediate, since most drops are
# one off and every second costs data. Repeated failures back off, and the last value repeats.
RECONNECT_SECONDS = (0, 1, 3, 10)


def reconnect_pause(failures):
    """
    Seconds to wait before the next attempt after this many failures in a row.
    """
    return RECONNECT_SECONDS[min(failures, len(RECONNECT_SECONDS)) - 1]


class Reconnect(Exception):
    """
    Raised by a subclass while handling a message when the connection must be rebuilt,
    for example after a skipped sequence number. The message is logged.
    """


class BookStream:
    """
    One websocket connection carrying every wanted contract. Runs forever
    once started, reconnecting when the connection drops, goes silent, or
    a subclass asks for it. Every failure starts a gap that ends when the
    next connection is subscribed, reported through on_gap so the recorder
    can mark the stretch. Subclasses set name and stale_seconds and
    implement the venue hooks below.
    """

    name = "venue"          # Used in log lines.
    stale_seconds = 120     # Reconnect after this long without a message that counts as data.

    def __init__(self, contract_ids, on_book, log=print, on_gap=None):
        self.wanted = set(contract_ids)
        self.on_book = on_book
        self.log = log
        self.on_gap = on_gap or (lambda start_ts, end_ts: None)    # Called with the gap's start and end times.
        self.down_since = None      # When the current gap began, or None while connected.
        self.failures = 0           # Failures in a row, reset once a connection is subscribed.
        self.books = {}
        self.commands = asyncio.Queue()     # Pending ("add" or "remove", [contract ids]) changes.

    def add(self, contract_ids):
        """
        Start streaming more contracts. Takes effect on the live connection.
        """
        new = set(contract_ids) - self.wanted
        self.wanted |= new
        if new:
            self.commands.put_nowait(("add", sorted(new)))

    def remove(self, contract_ids):
        """
        Stop streaming contracts and forget their books.
        """
        gone = set(contract_ids) & self.wanted
        self.wanted -= gone
        for contract_id in gone:
            self.books.pop(contract_id, None)
        if gone:
            self.commands.put_nowait(("remove", sorted(gone)))

    # VENUE HOOKS

    def connect(self):
        """
        Return the websocket connection context manager for this venue.
        """
        raise NotImplementedError

    async def subscribe(self, ws):
        """
        Send whatever subscribes a fresh connection to every wanted contract.
        """
        raise NotImplementedError

    async def send_command(self, ws, action, contract_ids):
        """
        Send the frame that applies one queued add or remove to the live connection.
        """
        raise NotImplementedError

    def handle(self, raw):
        """
        Apply one raw message. Return True when it counted as data, so the stale
        clock resets, and False for keepalive replies. Raise Reconnect to rebuild
        the connection.
        """
        raise NotImplementedError

    def reset(self):
        """
        Clear any per connection state before a new connection. Books are cleared by the loop.
        """

    async def keepalive(self, ws):
        """
        Send whatever the feed needs to keep the connection open, forever. Nothing by default.
        """
        await asyncio.sleep(float("inf"))

    # LOOP

    async def send_commands(self, ws):
        """
        Forward queued changes to the live connection, forever.
        """
        while True:
            action, contract_ids = await self.commands.get()
            await self.send_command(ws, action, contract_ids)

    async def read(self, ws):
        """
        Read messages until the connection fails or goes silent.
        """
        last_data = time.time()
        while True:
            remaining = self.stale_seconds - (time.time() - last_data)
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if self.handle(raw):
                last_data = time.time()

    async def run(self):
        """
        Connect, subscribe to every wanted contract, and process messages until
        the connection fails, then reconnect. Pending changes are dropped on
        connect because the fresh subscription already covers the wanted set.
        """
        while True:
            while not self.wanted:
                await asyncio.sleep(1)
            self.books = {}
            self.reset()
            while not self.commands.empty():
                self.commands.get_nowait()
            try:
                async with self.connect() as ws:
                    await self.subscribe(ws)
                    if self.down_since:
                        self.on_gap(self.down_since, now_iso())
                        self.down_since = None
                    self.failures = 0
                    helpers = [asyncio.create_task(self.keepalive(ws)), asyncio.create_task(self.send_commands(ws))]
                    try:
                        await self.read(ws)
                    finally:
                        for task in helpers:
                            task.cancel()
            except asyncio.TimeoutError:
                self.log(f"{self.name} stream silent for {self.stale_seconds}s, reconnecting")
            except Reconnect as e:
                self.log(f"{self.name} stream {e}, reconnecting")
            except (websockets.ConnectionClosed, OSError) as e:
                # For a closed connection the message carries the close code and reason from each side.
                self.log(f"{self.name} stream dropped ({type(e).__name__}: {str(e)[:100]}), reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A rejected handshake or a bad message must never end the stream for good.
                self.log(f"{self.name} stream failed ({type(e).__name__}: {str(e)[:120]}), reconnecting")
            if self.down_since is None:
                self.down_since = now_iso()
            self.failures += 1
            await asyncio.sleep(reconnect_pause(self.failures))
