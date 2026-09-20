"""
Polymarket US API client.

Two jobs. The query half reads the public events listing on the gateway
host and turns every open sports market into a Contract, one per market,
for the market's long side. The streaming half opens the signed markets
websocket and keeps a live book per market slug, replacing the whole
book on every message because the feed sends full snapshots. This is
the only file that knows Polymarket US field names and message formats.

Requests to the API host must be signed with the account's key. The key
id and secret live in the data folder, see KEY_ID_FILE and SECRET_KEY_FILE.
"""

import base64
import json
import time
import websockets
from api.bookstream import BookStream
from api.helper import get_json, float_or_none
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from db.models import Contract
from pathlib import Path
from util.timeutil import iso

GATEWAY = "https://gateway.polymarket.us/v1"    # Public catalog of events and markets.
API = "https://api.polymarket.us/v1"            # Signed requests for books and trading.
WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"
WS_CHUNK = 100          # Market slugs per subscription, the documented maximum.
STALE_SECONDS = 300     # The feed sends nothing while books are idle, so the limit is generous.
DATA = Path(__file__).resolve().parent.parent.parent / "data"
KEY_ID_FILE = DATA / "polymarket_us_key_id.txt"
SECRET_KEY_FILE = DATA / "polymarket_us_secret_key.txt"


# SIGNING

def signed_headers(method, path):
    """
    The three headers that authenticate a request. The signature is the
    account's Ed25519 key over the timestamp, method, and path.
    """
    key_id = KEY_ID_FILE.read_text().strip()
    secret = base64.b64decode(SECRET_KEY_FILE.read_text().strip())
    key = Ed25519PrivateKey.from_private_bytes(secret[:32])
    ts = str(int(time.time() * 1000))
    signature = base64.b64encode(key.sign(f"{ts}{method}{path}".encode())).decode()
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts, "X-PM-Signature": signature}


# QUERY

def fetch_events(page_size=500):
    """
    Every open event with its markets nested inside, following offset pagination.
    """
    events, offset = [], 0
    while True:
        page = get_json(f"{GATEWAY}/events", {"limit": page_size, "offset": offset, "closed": "false"})["events"]
        events.extend(page)
        if len(page) < page_size:
            return events
        offset += page_size


def contracts(sport, tags):
    """
    One Contract per open market on events carrying one of the tag slugs.
    The contract is the market's long side, which is Yes, Over, or the away team.
    """
    result = []
    for event in fetch_events():
        if not any(t.get("slug") in tags for t in event.get("tags", [])):
            continue
        for m in event.get("markets", []):
            if m.get("closed"):
                continue
            long_side = next((s for s in m.get("marketSides", []) if s.get("long")), {})
            result.append(Contract(
                venue="polymarket_us",
                contract_id=m["slug"],
                market_id=str(m.get("id")),
                event_id=event["slug"],
                series_id=event.get("seriesSlug"),
                sport=sport,
                event_title=event.get("title"),
                title=m.get("question") or m.get("title") or "",
                outcome=long_side.get("description") or "Yes",
                market_type=m.get("sportsMarketType"),
                line=float_or_none(m.get("line")),
                rules=m.get("description"),
                start_time=iso(event.get("startTime")) if event.get("gameId") else None,
                close_time=iso(m.get("endDate")),
                fee_info={"feeCoefficient": m.get("feeCoefficient")},
            ))
    return result


# STREAMING

def levels(entries, reverse):
    """
    Turn the feed's price levels into [price, size] lists, best first.
    """
    parsed = [[float(e["px"]["value"]), float(e["qty"])] for e in entries or []]
    return sorted((lv for lv in parsed if lv[1] > 0), key=lambda lv: lv[0], reverse=reverse)


class PolymarketUSBookStream(BookStream):
    """
    The signed markets websocket. Each message carries a market's whole
    book, so the local copy is replaced rather than patched. Subscriptions
    are sent in groups of WS_CHUNK slugs. The feed documents no
    unsubscribe, so removed slugs are simply ignored until the next connect.
    """

    name = "polymarket_us"
    stale_seconds = STALE_SECONDS

    def reset(self):
        self.request_id = 0

    def connect(self):
        return websockets.connect(WS_URL, additional_headers=signed_headers("GET", WS_PATH), open_timeout=20, max_size=None)

    async def subscribe(self, ws):
        await self.send_subscriptions(ws, sorted(self.wanted))

    async def send_command(self, ws, action, slugs):
        if action == "add":
            await self.send_subscriptions(ws, slugs)

    async def send_subscriptions(self, ws, slugs):
        """
        Subscribe to full market data for the slugs, WS_CHUNK at a time.
        """
        for i in range(0, len(slugs), WS_CHUNK):
            self.request_id += 1
            await ws.send(json.dumps({"subscribe": {"requestId": f"md-{self.request_id}",
                                                    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                                    "marketSlugs": slugs[i:i + WS_CHUNK]}}))

    def handle(self, raw):
        m = json.loads(raw)
        data = m.get("marketData")
        if not data:
            return False
        slug = data.get("marketSlug")
        if slug not in self.wanted:
            return True
        bids, asks = levels(data.get("bids"), reverse=True), levels(data.get("offers"), reverse=False)
        self.books[slug] = {"bids": bids, "asks": asks}
        self.on_book(slug, bids, asks)
        return True
