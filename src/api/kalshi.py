"""
Kalshi API client.

Two jobs. The query half walks sports series to events to markets on the
public API and turns every open market into a Contract. The streaming
half opens one websocket with a signed API key, subscribes to order book
updates, and keeps a live book for each ticker restated from the Yes side
so it matches Polymarket's shape. Tickers can be added and removed while
the connection runs. This is the only file that knows Kalshi's field
names and message formats.
"""

import asyncio
import base64
import json
import time
import websockets
from api.helper import get_json, float_or_none
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from db.models import Contract
from pathlib import Path
from util.timeutil import iso

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SLEEP = 0.12   # Seconds between calls, to stay under the public rate limit.
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
STALE_SECONDS = 300     # A connection that sends nothing for this long is treated as dead.
DATA = Path(__file__).resolve().parent.parent.parent / "data"
KEY_ID_FILE = DATA / "kalshi_key_id.txt"
PRIVATE_KEY_FILE = DATA / "kalshi_private_key.pem"


# QUERY

def paged(path, params, key):
    """
    Follow Kalshi's cursor pagination and return every item under key.
    """
    items, cursor = [], None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = get_json(f"{BASE}{path}", p)
        items.extend(data.get(key, []))
        cursor = data.get("cursor")
        time.sleep(SLEEP)
        if not cursor:
            return items


def fetch_series(prefixes, tickers=()):
    """
    Sports series whose ticker starts with one of the prefixes, or is listed exactly in tickers.
    """
    all_series = paged("/series", {"category": "Sports", "limit": 200}, "series")
    return [s for s in all_series if s["ticker"].startswith(tuple(prefixes)) or s["ticker"] in tickers]


def fetch_events(series_ticker):
    """
    Open events for a series, with their markets nested inside.
    """
    return paged("/events", {
        "series_ticker": series_ticker, "status": "open",
        "with_nested_markets": "true", "limit": 200,
    }, "events")


def strict_line(m):
    """
    The market's line, restated so Yes always means strictly more than the line.
    Kalshi marks 'at least N' markets as greater_or_equal with a whole number
    strike, and at least N is the same as more than N minus a half.
    """
    line = float_or_none(m.get("floor_strike"))
    if line is None:
        line = float_or_none(m.get("cap_strike"))
    if line is not None and m.get("strike_type") == "greater_or_equal":
        line -= 0.5
    return line


def contracts(sport, prefixes, tickers=()):
    """
    One Contract per Kalshi market, meaning its Yes side.
    """
    result = []
    for series in fetch_series(prefixes, tickers):
        fee_info = {
            "fee_type": series.get("fee_type"),
            "fee_multiplier": series.get("fee_multiplier"),
        }
        for event in fetch_events(series["ticker"]):
            for m in event.get("markets", []):
                if m.get("status") not in (None, "open", "active"):
                    continue
                rules = " ".join(filter(None, [m.get("rules_primary"), m.get("rules_secondary")])) or None
                line = strict_line(m)
                result.append(Contract(
                    venue="kalshi",
                    contract_id=m["ticker"],
                    market_id=m["ticker"],
                    event_id=m.get("event_ticker") or event["event_ticker"],
                    series_id=series["ticker"],
                    sport=sport,
                    event_title=event.get("title"),
                    title=m.get("title") or "",
                    outcome=m.get("yes_sub_title") or "Yes",
                    market_type=None,
                    line=line,
                    rules=rules,
                    start_time=None,
                    close_time=iso(m.get("close_time")),
                    fee_info=fee_info,
                ))
    return result


# STREAMING

def ws_headers():
    """
    Signed headers for opening the websocket. Kalshi wants the timestamp, the
    method, and the path signed with the account's RSA key.
    """
    key_id = KEY_ID_FILE.read_text().strip()
    key = serialization.load_pem_private_key(PRIVATE_KEY_FILE.read_bytes(), password=None)
    ts = str(int(time.time() * 1000))
    message = (ts + "GET" + WS_PATH).encode()
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def update_frame(message_id, sid, tickers, action):
    """
    The command that adds or removes tickers on a live subscription. action is
    'add_markets' or 'delete_markets'.
    """
    return {"id": message_id, "cmd": "update_subscription",
            "params": {"sids": [sid], "market_tickers": tickers, "action": action}}


class BookStream:
    """
    One websocket connection carrying every wanted ticker. Keeps a live book
    per ticker and calls on_book(ticker, bids, asks) after each change. Books
    are given from the Yes side, best first, so they look the same as
    Polymarket's. A resting No order at price p is a Yes ask at 1 minus p.
    Runs forever once started, reconnecting when the connection drops, goes
    silent, or skips a sequence number.
    """

    def __init__(self, tickers, on_book, log=print):
        self.wanted = set(tickers)
        self.on_book = on_book
        self.log = log
        self.books = {}
        self.commands = asyncio.Queue()     # Pending ("add_markets" or "delete_markets", [tickers]).
        self.sid = None                     # The live subscription id, needed for update commands.
        self.subscribed = asyncio.Event()   # Set once the subscribe acknowledgement arrives.

    def add(self, tickers):
        """
        Start streaming more tickers. Takes effect on the live connection.
        """
        new = set(tickers) - self.wanted
        self.wanted |= new
        if new:
            self.commands.put_nowait(("add_markets", sorted(new)))

    def remove(self, tickers):
        """
        Stop streaming tickers and forget their books.
        """
        gone = set(tickers) & self.wanted
        self.wanted -= gone
        for ticker in gone:
            self.books.pop(ticker, None)
        if gone:
            self.commands.put_nowait(("delete_markets", sorted(gone)))

    def apply(self, m):
        """
        Update the local books from one feed message and report the changed ticker.
        """
        kind, body = m.get("type"), m.get("msg") or {}
        ticker = body.get("market_ticker")
        if kind == "subscribed":
            self.sid = body.get("sid")
            self.subscribed.set()
            return
        if kind == "orderbook_snapshot" and ticker in self.wanted:
            self.books[ticker] = {
                "yes": {float(p): float(s) for p, s in body.get("yes_dollars_fp") or []},
                "no": {float(p): float(s) for p, s in body.get("no_dollars_fp") or []},
            }
        elif kind == "orderbook_delta" and ticker in self.books:
            side = self.books[ticker][body["side"]]
            price = float(body["price_dollars"])
            # Round to cents so summing many deltas does not leave floating point residue.
            side[price] = round(side.get(price, 0.0) + float(body["delta_fp"]), 2)
            if side[price] <= 0:
                del side[price]
        else:
            if kind == "error":
                self.log(f"kalshi stream error {body}")
            return
        b = self.books[ticker]
        bids = [[p, s] for p, s in sorted(b["yes"].items(), reverse=True) if s > 0]
        asks = [[round(1 - p, 4), s] for p, s in sorted(b["no"].items(), reverse=True) if s > 0]
        self.on_book(ticker, bids, asks)

    async def send_commands(self, ws):
        """
        Forward queued update commands to the live subscription, forever.
        """
        message_id = 2
        while True:
            action, tickers = await self.commands.get()
            await self.subscribed.wait()
            await ws.send(json.dumps(update_frame(message_id, self.sid, tickers, action)))
            message_id += 1

    async def run(self):
        """
        Connect, subscribe to every wanted ticker, and process messages until
        the connection fails, then reconnect. Pending commands are dropped on
        connect because the fresh subscription already covers the wanted set.
        """
        while True:
            while not self.wanted:
                await asyncio.sleep(1)
            self.books, self.sid, last_seq = {}, None, None
            self.subscribed.clear()
            while not self.commands.empty():
                self.commands.get_nowait()
            try:
                async with websockets.connect(WS_URL, additional_headers=ws_headers(), open_timeout=20, max_size=None) as ws:
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                              "params": {"channels": ["orderbook_delta"], "market_tickers": sorted(self.wanted)}}))
                    sender = asyncio.create_task(self.send_commands(ws))
                    try:
                        while True:
                            # Kalshi sends nothing while books are idle, so the limit is generous.
                            raw = await asyncio.wait_for(ws.recv(), timeout=STALE_SECONDS)
                            m = json.loads(raw)
                            seq = m.get("seq")
                            if seq is not None:
                                if last_seq is not None and seq != last_seq + 1:
                                    self.log(f"kalshi stream skipped from seq {last_seq} to {seq}, reconnecting")
                                    break
                                last_seq = seq
                            self.apply(m)
                    finally:
                        sender.cancel()
            except asyncio.TimeoutError:
                self.log(f"kalshi stream silent for {STALE_SECONDS}s, reconnecting")
            except (websockets.ConnectionClosed, OSError) as e:
                self.log(f"kalshi stream dropped ({type(e).__name__}), reconnecting")
            await asyncio.sleep(3)
