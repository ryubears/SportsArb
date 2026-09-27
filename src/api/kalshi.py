"""
Kalshi API client.

Three jobs. The query half walks sports series to events to markets on
the public API, turns every open market into a Contract, and reads the
account's balance. The streaming half opens one websocket with a signed
API key, subscribes to order book updates, and keeps a live book for each
ticker restated from the Yes side so it matches Polymarket US's shape.
Tickers can be added and removed while the connection runs. The trading
half sends signed orders for the live executor. This is the only file
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
from common.jsonutil import float_or_none, float_or_zero
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


def signed_request(method, path, body=None, params=None):
    """
    A signed call to the API at a path under BASE, for example
    '/portfolio/balance'. The signature covers the path without its query.
    """
    url = BASE + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    return send_json(method, url, signed_headers(method, BASE_PATH + path), body)


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


def balance():
    """
    Dollars available for trading on the account.
    """
    answer = signed_request("GET", "/portfolio/balance")
    return float_or_zero(answer["balance_dollars"]) if "balance_dollars" in answer else answer["balance"] / 100


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
    from the Yes side, best first, so they look the same as Polymarket US's.
    The subscription asks for use_yes_price, so a resting No order arrives
    already priced as the Yes ask it is, rather than at the No price, which
    Kalshi's default did until it announced it would flip. Every message
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
                                  "params": {"channels": ["orderbook_delta"], "market_tickers": sorted(self.wanted), "use_yes_price": True}}))

    async def send_command(self, ws, action, tickers):
        await self.subscribed.wait()
        kalshi_action = "add_markets" if action == "add" else "delete_markets"
        await ws.send(json.dumps(update_frame(self.message_id, self.sid, tickers, kalshi_action)))
        self.message_id += 1

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
        asks = [[p, s] for p, s in sorted(b["no"].items()) if s > 0]
        self.on_book(ticker, bids, asks)

    def handle(self, raw):
        m = json.loads(raw)
        seq = m.get("seq")
        if seq is not None:
            if self.last_seq is not None and seq != self.last_seq + 1:
                raise Reconnect(f"skipped from seq {self.last_seq} to {seq}")
            self.last_seq = seq
        self.apply(m)
        return True


# TRADING

# The book side of an order for each action on each outcome. Orders are quoted on the yes side:
# buying no is selling yes, and selling no back is buying yes.
BOOK_SIDES = {("buy", "yes"): "bid", ("sell", "no"): "bid", ("buy", "no"): "ask", ("sell", "yes"): "ask"}


def order_body(ticker, action, outcome, quantity, price, client_id):
    """
    An immediate or cancel limit order for quantity contracts of one side of
    a market, at price or better for that side. The order endpoint quotes
    every order on the yes side, so a no side price p is sent as 1 - p. A
    sale is reduce only, so it can close what is held but never open the
    other side, and an order that would trade against one of our own is
    cancelled rather than filled.
    """
    yes_price = price if outcome == "yes" else 1 - price
    body = {"ticker": ticker, "client_order_id": client_id, "side": BOOK_SIDES[(action, outcome)], "count": str(quantity),
            "price": f"{yes_price:.4f}", "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
    if action == "sell":
        body["reduce_only"] = True
    return body


def place_order(ticker, action, outcome, quantity, price, client_id):
    """
    Send an immediate or cancel limit order and return what came back as an
    Answer. action is 'buy' or 'sell', outcome 'yes' or 'no', and
    price the worst price per contract accepted for that outcome. The answer
    gives the contracts filled, their average price on the yes side, so a no
    side fill at p cost 1 - p, and the average fee per contract. Contracts
    are whole in our books, so a fractional fill counts its whole contracts
    and says so in the note.
    """
    try:
        answer = signed_request("POST", "/portfolio/events/orders", order_body(ticker, action, outcome, quantity, price, client_id))
    except RequestFailed as e:
        return orders.refused(e) if e.status < 500 else orders.unknown(e)
    except Exception as e:
        return orders.unknown(e)
    exact = float_or_zero(answer.get("fill_count"))
    filled = int(exact)
    note = f"fractional fill of {exact} contracts" if exact != filled else None
    yes_price = float_or_zero(answer.get("average_fill_price"))
    traded = filled * (yes_price if outcome == "yes" else 1 - yes_price)
    fees = filled * float_or_zero(answer.get("average_fee_paid"))
    paid = traded + fees if action == "buy" else traded - fees
    return orders.Answer(answer.get("order_id"), orders.status(filled, quantity), filled, paid, fees, note, answer)
