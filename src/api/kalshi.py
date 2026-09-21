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
from api.bookstream import BookStream, Reconnect
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


def close_time(m):
    """
    When the contract stops trading, or settles if the venue expects that
    sooner. Kalshi's close_time on futures can be a placeholder years out,
    while expected_expiration_time carries the real settlement date.
    """
    times = [t for t in (iso(m.get("close_time")), iso(m.get("expected_expiration_time"))) if t]
    return min(times) if times else None


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
                    close_time=close_time(m),
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


class KalshiBookStream(BookStream):
    """
    Kalshi's order book channel over a signed connection. Books are given
    from the Yes side, best first, so they look the same as Polymarket's. A
    resting No order at price p is a Yes ask at 1 minus p. Every message
    counts as data because the feed has no keepalive replies, and the
    stale limit is generous because the feed sends nothing while books
    are idle. A skipped sequence number forces a reconnect.
    """

    name = "kalshi"
    stale_seconds = STALE_SECONDS

    def reset(self):
        self.sid = None                     # The live subscription id, needed for update commands.
        self.last_seq = None
        self.subscribed = asyncio.Event()   # Set once the subscribe acknowledgement arrives.
        self.message_id = 2

    def connect(self):
        # No receive queue limit, so a busy loop delays our timestamps instead of stalling the socket.
        return websockets.connect(WS_URL, additional_headers=ws_headers(), open_timeout=20, max_size=None, max_queue=None)

    async def subscribe(self, ws):
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta"], "market_tickers": sorted(self.wanted)}}))

    async def send_command(self, ws, action, tickers):
        await self.subscribed.wait()
        kalshi_action = "add_markets" if action == "add" else "delete_markets"
        await ws.send(json.dumps(update_frame(self.message_id, self.sid, tickers, kalshi_action)))
        self.message_id += 1

    def handle(self, raw):
        m = json.loads(raw)
        seq = m.get("seq")
        if seq is not None:
            if self.last_seq is not None and seq != self.last_seq + 1:
                raise Reconnect(f"skipped from seq {self.last_seq} to {seq}")
            self.last_seq = seq
        self.apply(m)
        return True

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
