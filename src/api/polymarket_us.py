"""
Polymarket US API client.

Two jobs. The query half reads the public events listing on the gateway
host, filtered by sport tag, and turns every open market into a Contract,
one per market, for the market's long side. The streaming half opens the signed markets
websocket and keeps a live book per market slug, replacing the whole
book on every message because the feed sends full snapshots. This is
the only file that knows Polymarket US field names and message formats.

Requests to the API host must be signed with the account's key. The key
id and secret live in the data folder, see KEY_ID_FILE and SECRET_KEY_FILE.
"""

import base64
import json
import time
from api.bookstream import BookStream
from api.http import get_json
from common.jsonutil import float_or_none
from common.paths import DATA_DIR
from common.timeutil import iso
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from db.models import Contract

GATEWAY = "https://gateway.polymarket.us/v1"    # Public catalog of events and markets.
API = "https://api.polymarket.us/v1"            # Signed requests for books and trading.
WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"
WS_CHUNK = 100          # Market slugs per subscription, the documented maximum.
WS_SUBSCRIPTIONS = 10   # Subscriptions per connection. The feed refuses an eleventh with 'max subscriptions per connection reached'.
WS_DEBOUNCE = True      # Ask the feed to batch updates. Cuts bandwidth by a third, and the recorder writes once a second anyway.
KEY_ID_FILE = DATA_DIR / "polymarket_us_key_id.txt"
SECRET_KEY_FILE = DATA_DIR / "polymarket_us_secret_key.txt"


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

def fetch_events(tag_slug, page_size=500):
    """
    Every open event carrying the tag, with its markets nested inside, following offset pagination.
    """
    events, offset = [], 0
    while True:
        page = get_json(f"{GATEWAY}/events", {"tag_slug": tag_slug, "limit": page_size, "offset": offset, "closed": "false"})["events"]
        events.extend(page)
        if len(page) < page_size:
            return events
        offset += page_size


def results(event_slugs):
    """
    Settlement results for every market on the events, as {market slug:
    (result, settled_at)} with result 'yes' when the long side paid out and
    'no' when it did not. Markets not yet resolved are left out.
    """
    out = {}
    for slug in set(event_slugs):
        for event in get_json(f"{GATEWAY}/events", {"slug": slug}).get("events", []):
            for m in event.get("markets", []):
                long_side = next((s for s in m.get("marketSides", []) if s.get("long")), None)
                if m.get("status") == "MARKET_STATUS_RESOLVED" and long_side and long_side.get("price") in ("0", "1"):
                    out[m["slug"]] = ("yes" if long_side["price"] == "1" else "no", iso(m.get("endDate")))
    return out


def contracts(sport, tags):
    """
    One Contract per open market on events carrying one of the tag slugs.
    The contract is the market's long side, which is Yes, Over, or the away team.
    """
    result, seen_events = [], set()
    for event in (e for tag in tags for e in fetch_events(tag)):
        if event["slug"] in seen_events:
            continue
        seen_events.add(event["slug"])
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
    are sent in groups of WS_CHUNK slugs, batched when WS_DEBOUNCE is set.
    The feed documents no unsubscribe, so removed slugs are simply ignored
    until the next connect. A connection carries at most capacity slugs,
    so the recorder opens more connections for a larger set.
    """

    name = "polymarket_us"
    capacity = WS_CHUNK * WS_SUBSCRIPTIONS

    def reset(self):
        self.request_id = 0

    def connect(self):
        return self.open_connection(WS_URL, signed_headers("GET", WS_PATH))

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
                                                    "marketSlugs": slugs[i:i + WS_CHUNK],
                                                    "responsesDebounced": WS_DEBOUNCE}}))

    def handle(self, raw):
        m = json.loads(raw)
        data = m.get("marketData")
        if not data:
            if m.get("error"):
                self.log(f"polymarket_us stream error {m['error']} on {m.get('requestId')}")
            return False
        slug = data.get("marketSlug")
        if slug not in self.wanted:
            return True
        bids, asks = levels(data.get("bids"), reverse=True), levels(data.get("offers"), reverse=False)
        self.books[slug] = {"bids": bids, "asks": asks}
        self.on_book(slug, bids, asks)
        return True
