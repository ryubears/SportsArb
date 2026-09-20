"""
Polymarket API client.

Two jobs. The query half reads the public metadata API and turns every
open sports market into Contracts. The streaming half keeps one public
websocket connection carrying every wanted token, keeps a live order
book for each, and hands every change to a callback. Tokens can be added
and removed while the connection runs. This is the only file that knows
Polymarket's field names and message formats.
"""

import asyncio
import json
import time
import websockets
from api.helper import get_json, float_or_none
from db.models import Contract
from util import jsonutil
from util.timeutil import iso

GAMMA = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_CHUNK = 500          # Tokens per subscribe frame. Above this the feed stops sending snapshots.
WS_PING_SECONDS = 10    # The feed drops idle connections unless it hears a PING.
STALE_SECONDS = 120     # A connection that sends no book data for this long is dead, even if it still answers pings.


# QUERY

def fetch_events(tag_slug, page_size=100):
    """
    All active, unclosed events with a tag, following offset pagination.
    """
    events, offset = [], 0
    while True:
        page = get_json(f"{GAMMA}/events", {
            "tag_slug": tag_slug, "active": "true", "closed": "false",
            "limit": page_size, "offset": offset,
        })
        events.extend(page)
        if len(page) < page_size:
            return events
        offset += page_size


def contracts(sport, tags):
    """
    One Contract per outcome token for every event under the given tags.
    The literal 'No' side of Yes/No markets is skipped.
    """
    result = []
    seen_events = set()
    for tag in tags:
        for event in fetch_events(tag):
            if event["slug"] in seen_events:
                continue
            seen_events.add(event["slug"])
            for m in event.get("markets", []):
                if m.get("closed"):
                    continue
                outcomes = jsonutil.parse(m.get("outcomes"), [])
                token_ids = jsonutil.parse(m.get("clobTokenIds"), [])
                if len(outcomes) != len(token_ids) or not token_ids:
                    continue
                fee_info = {
                    "feesEnabled": m.get("feesEnabled"),
                    "feeType": m.get("feeType"),
                    "takerBaseFee": m.get("takerBaseFee"),
                    "makerBaseFee": m.get("makerBaseFee"),
                    "feeSchedule": m.get("feeSchedule"),
                }
                for outcome, token_id in zip(outcomes, token_ids):
                    if outcome == "No":
                        continue
                    result.append(Contract(
                        venue="polymarket",
                        contract_id=str(token_id),
                        market_id=m["conditionId"],
                        event_id=event["slug"],
                        series_id=None,
                        sport=sport,
                        event_title=event.get("title"),
                        title=m.get("question") or "",
                        outcome=outcome,
                        market_type=m.get("sportsMarketType"),
                        line=float_or_none(m.get("line")),
                        rules=m.get("description"),
                        start_time=iso(m.get("gameStartTime")),
                        close_time=iso(m.get("endDate")),
                        fee_info=fee_info,
                    ))
    return result


# STREAMING

def subscribe_frames(token_ids):
    """
    The frames that subscribe a fresh connection to these tokens. The feed
    only sends snapshots for up to WS_CHUNK tokens per frame, so the set is
    split. The first frame opens the market channel and the rest add to it.
    """
    chunks = [token_ids[i:i + WS_CHUNK] for i in range(0, len(token_ids), WS_CHUNK)]
    frames = [{"assets_ids": chunks[0], "type": "market"}] if chunks else []
    frames += [{"assets_ids": chunk, "operation": "subscribe"} for chunk in chunks[1:]]
    return frames


def sorted_levels(levels, reverse):
    """
    Turn a {price: size} dict into a list of [price, size] with the best price first.
    """
    return [[p, s] for p, s in sorted(levels.items(), reverse=reverse) if s > 0]


async def send_pings(ws):
    """
    Send the text PING the feed expects, forever.
    """
    while True:
        await asyncio.sleep(WS_PING_SECONDS)
        await ws.send("PING")


class BookStream:
    """
    One websocket connection carrying every wanted token. Keeps a live book
    per token and calls on_book(token_id, bids, asks) after each change,
    with bids and asks as lists of [price, size], best first. Runs forever
    once started, reconnecting when the connection drops or goes silent.
    """

    def __init__(self, token_ids, on_book, log=print):
        self.wanted = set(token_ids)
        self.on_book = on_book
        self.log = log
        self.books = {}
        self.commands = asyncio.Queue()     # Pending ("subscribe" or "unsubscribe", [token ids]) frames.

    def add(self, token_ids):
        """
        Start streaming more tokens. Takes effect on the live connection.
        """
        new = set(token_ids) - self.wanted
        self.wanted |= new
        if new:
            self.commands.put_nowait(("subscribe", sorted(new)))

    def remove(self, token_ids):
        """
        Stop streaming tokens and forget their books.
        """
        gone = set(token_ids) & self.wanted
        self.wanted -= gone
        for token in gone:
            self.books.pop(token, None)
        if gone:
            self.commands.put_nowait(("unsubscribe", sorted(gone)))

    def emit(self, token):
        """
        Hand the sorted book for one token to the callback.
        """
        b = self.books[token]
        self.on_book(token, sorted_levels(b["bids"], reverse=True), sorted_levels(b["asks"], reverse=False))

    def apply(self, m):
        """
        Update the local books from one feed message and report each changed token.
        """
        kind = m.get("event_type")
        if kind == "book":
            token = m["asset_id"]
            if token not in self.wanted:
                return
            self.books[token] = {
                "bids": {float(x["price"]): float(x["size"]) for x in m["bids"]},
                "asks": {float(x["price"]): float(x["size"]) for x in m["asks"]},
            }
            self.emit(token)
        elif kind == "price_change":
            changed = set()
            for change in m.get("price_changes", []):
                token = change["asset_id"]
                if token not in self.books:
                    continue
                side = "bids" if change["side"] == "BUY" else "asks"
                self.books[token][side][float(change["price"])] = float(change["size"])
                changed.add(token)
            for token in changed:
                self.emit(token)

    async def send_commands(self, ws):
        """
        Forward queued subscribe and unsubscribe frames to the connection, forever.
        """
        while True:
            operation, token_ids = await self.commands.get()
            for i in range(0, len(token_ids), WS_CHUNK):
                await ws.send(json.dumps({"assets_ids": token_ids[i:i + WS_CHUNK], "operation": operation}))

    async def run(self):
        """
        Connect, subscribe to every wanted token, and process messages until
        the connection fails, then reconnect. Pending commands are dropped on
        connect because the fresh subscription already covers the wanted set.
        """
        while True:
            while not self.wanted:
                await asyncio.sleep(1)
            self.books = {}
            while not self.commands.empty():
                self.commands.get_nowait()
            try:
                async with websockets.connect(WS_URL, open_timeout=20, max_size=None) as ws:
                    for frame in subscribe_frames(sorted(self.wanted)):
                        await ws.send(json.dumps(frame))
                    helpers = [asyncio.create_task(send_pings(ws)), asyncio.create_task(self.send_commands(ws))]
                    last_data = time.time()
                    try:
                        while True:
                            # Only book data counts. A stalled feed can keep answering pings.
                            remaining = STALE_SECONDS - (time.time() - last_data)
                            if remaining <= 0:
                                raise asyncio.TimeoutError
                            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                            if raw == "PONG":
                                continue
                            last_data = time.time()
                            messages = json.loads(raw)
                            for m in messages if isinstance(messages, list) else [messages]:
                                self.apply(m)
                    finally:
                        for task in helpers:
                            task.cancel()
            except asyncio.TimeoutError:
                self.log(f"polymarket stream silent for {STALE_SECONDS}s, reconnecting")
            except (websockets.ConnectionClosed, OSError) as e:
                self.log(f"polymarket stream dropped ({type(e).__name__}), reconnecting")
            await asyncio.sleep(3)
