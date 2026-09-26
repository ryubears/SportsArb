"""
Kalshi API client.

Three jobs. The query half walks sports series to events to markets on
the public API and turns every open market into a Contract. The trading
half reads the account's balance and sends signed orders for the live
executor. The streaming half opens one websocket with a signed API key,
subscribes to order book updates, and keeps a live book for each ticker
restated from the Yes side so it matches Polymarket US's shape. Tickers
can be added and removed while the connection runs. This is the only file
that knows Kalshi's field names and message formats.
"""

import asyncio
import base64
import functools
import json
import time
import urllib.parse
from api import orders
from api.bookstream import BookStream, Reconnect
from api.http import RequestFailed, get_json, send_json
from common.jsonutil import float_or_none
from common.paths import DATA_DIR
from common.timeutil import iso
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from db.models import Contract

BASE = "https://api.elections.kalshi.com/trade-api/v2"
BASE_PATH = "/trade-api/v2"     # BASE's path, which a signed request's signature covers.
SLEEP = 0.12   # Seconds between paged calls, to stay under the public rate limit.
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
KEY_ID_FILE = DATA_DIR / "kalshi_key_id.txt"
PRIVATE_KEY_FILE = DATA_DIR / "kalshi_private_key.pem"
RESULTS_BATCH = 50   # Tickers per markets call when looking up results.


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
        if not cursor:
            return items
        time.sleep(SLEEP)


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


def results(tickers):
    """
    Settlement results for the tickers, as {ticker: (result, settled_at)} with
    result 'yes' or 'no'. Markets not yet finalized are left out. The
    markets endpoint takes a list of tickers, so this costs one call per
    RESULTS_BATCH tickers.
    """
    out = {}
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), RESULTS_BATCH):
        batch = tickers[i:i + RESULTS_BATCH]
        for m in get_json(f"{BASE}/markets", {"tickers": ",".join(batch), "limit": len(batch)}).get("markets", []):
            if m.get("status") == "finalized" and m.get("result") in ("yes", "no"):
                out[m["ticker"]] = (m["result"], iso(m.get("settlement_ts")))
        if i + RESULTS_BATCH < len(tickers):
            time.sleep(SLEEP)
    return out


# SIGNING

@functools.cache
def credentials():
    """
    The account's key id and RSA private key, read from the data folder once.
    """
    return KEY_ID_FILE.read_text().strip(), serialization.load_pem_private_key(PRIVATE_KEY_FILE.read_bytes(), password=None)


def signed_headers(method, path):
    """
    The three headers that authenticate a request. Kalshi wants the timestamp,
    the method, and the path signed with the account's RSA key. The websocket
    handshake and the trading endpoints use the same scheme.
    """
    key_id, key = credentials()
    ts = str(int(time.time() * 1000))
    message = (ts + method + path).encode()
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


# TRADING

def signed_request(method, path, body=None, params=None):
    """
    A signed call to the trading API at a path under BASE, for example
    '/portfolio/balance'. The signature covers the path without its query.
    """
    url = BASE + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    return send_json(method, url, signed_headers(method, BASE_PATH + path), body)


def dollars(value):
    """
    A fixed point dollar string from the API as a float, zero when missing.
    """
    return float(value) if value not in (None, "") else 0.0


def balance():
    """
    Dollars available for trading on the account.
    """
    return signed_request("GET", "/portfolio/balance")["balance"] / 100


def order_body(ticker, action, outcome, quantity, price, client_id):
    """
    An immediate or cancel limit order for quantity contracts of one side of
    a market, at price or better for that side. A sale is reduce only, so it
    can close what is held but never open the other side.
    """
    body = {"ticker": ticker, "client_order_id": client_id, "action": action, "side": outcome, "count": quantity,
            "type": "limit", "time_in_force": "immediate_or_cancel", f"{outcome}_price_dollars": f"{price:.4f}"}
    if action == "sell":
        body["reduce_only"] = True
    return body


def fills(order_id):
    """
    The fills of one order, as [(contracts, price)] with the price of the side the order traded.
    """
    out = []
    for f in signed_request("GET", "/portfolio/fills", params={"order_id": order_id}).get("fills", []):
        side = f["side"]
        price = f.get(f"{side}_price_dollars") or f.get(f"{side}_price_fixed")
        out.append((int(f["count"]), float(price) if price is not None else f[f"{side}_price"] / 100))
    return out


def place_order(ticker, action, outcome, quantity, price, client_id):
    """
    Send an immediate or cancel limit order and return what came back as an
    orders.Answer. action is 'buy' or 'sell', outcome 'yes' or 'no', and
    price the worst price per contract accepted for that outcome. What an
    order traded at is read from its fills, which give each fill's price
    for the side traded, and its fees from the order. When the fills are not
    all visible yet, a buy is costed from the order's fill cost and a sale
    at its limit, the least it can have fetched.
    """
    try:
        order = signed_request("POST", "/portfolio/orders", order_body(ticker, action, outcome, quantity, price, client_id))["order"]
    except RequestFailed as e:
        return orders.refused(e) if e.status < 500 else orders.unknown(e)
    except Exception as e:
        return orders.unknown(e)
    filled = int(order.get("fill_count") or 0)
    fees = dollars(order.get("taker_fees_dollars")) + dollars(order.get("maker_fees_dollars"))
    response = {"order": order}
    traded = 0.0
    if filled:
        try:
            found = fills(order["order_id"])
        except Exception as e:
            found, response["fills_error"] = [], repr(e)
        response["fills"] = found
        if sum(n for n, _ in found) == filled:
            traded = sum(n * p for n, p in found)
        elif action == "buy":
            traded = dollars(order.get("taker_fill_cost_dollars")) + dollars(order.get("maker_fill_cost_dollars"))
        else:
            traded = filled * price
    paid = traded + fees if action == "buy" else traded - fees
    return orders.Answer(order.get("order_id"), orders.status(filled, quantity), filled, paid, fees, None, response)


# STREAMING

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
    from the Yes side, best first, so they look the same as Polymarket US's. A
    resting No order at price p is a Yes ask at 1 minus p. Every message
    counts as data because the feed has no keepalive replies. A skipped
    sequence number forces a reconnect.
    """

    name = "kalshi"

    def reset(self):
        self.sid = None                     # The live subscription id, needed for update commands.
        self.last_seq = None
        self.subscribed = asyncio.Event()   # Set once the subscribe acknowledgement arrives.
        self.message_id = 2

    def connect(self):
        return self.open_connection(WS_URL, signed_headers("GET", WS_PATH))

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
        if kind == "error":
            self.log(f"kalshi stream error {body}")
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
            return
        b = self.books[ticker]
        bids = [[p, s] for p, s in sorted(b["yes"].items(), reverse=True) if s > 0]
        asks = [[round(1 - p, 4), s] for p, s in sorted(b["no"].items(), reverse=True) if s > 0]
        self.on_book(ticker, bids, asks)
