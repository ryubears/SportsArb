"""
Polymarket API client.

Two jobs. The query half reads the public metadata API and turns every
open sports market into Contracts. The streaming half subscribes to the
public websocket and keeps a live order book for each token, handing
every change to a callback. This is the only file that knows
Polymarket's field names and message formats.
"""

import asyncio
import json
import websockets
from api.helper import get_json, float_or_none
from db.models import Contract
from util import jsonutil
from util.timeutil import iso

GAMMA = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_CHUNK = 500          # Tokens per connection. Above this the feed stops sending snapshots.
WS_PING_SECONDS = 10    # The feed drops idle connections unless it hears a PING.


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

def sorted_levels(levels, reverse):
    """
    Turn a {price: size} dict into a list of [price, size] with the best price first.
    """
    return [[p, s] for p, s in sorted(levels.items(), reverse=reverse) if s > 0]


def emit(token, books, on_book):
    """
    Hand the sorted book for one token to the callback.
    """
    b = books[token]
    on_book(token, sorted_levels(b["bids"], reverse=True), sorted_levels(b["asks"], reverse=False))


def apply_message(m, books, wanted, on_book):
    """
    Update the local books from one feed message and report each changed token.
    """
    kind = m.get("event_type")
    if kind == "book":
        token = m["asset_id"]
        if token not in wanted:
            return
        books[token] = {
            "bids": {float(x["price"]): float(x["size"]) for x in m["bids"]},
            "asks": {float(x["price"]): float(x["size"]) for x in m["asks"]},
        }
        emit(token, books, on_book)
    elif kind == "price_change":
        changed = set()
        for change in m.get("price_changes", []):
            token = change["asset_id"]
            if token not in wanted or token not in books:
                continue
            side = "bids" if change["side"] == "BUY" else "asks"
            books[token][side][float(change["price"])] = float(change["size"])
            changed.add(token)
        for token in changed:
            emit(token, books, on_book)


async def send_pings(ws):
    """
    Send the text PING the feed expects, forever.
    """
    while True:
        await asyncio.sleep(WS_PING_SECONDS)
        await ws.send("PING")


async def stream_chunk(token_ids, on_book, log):
    """
    One connection for up to WS_CHUNK tokens.
    """
    wanted = set(token_ids)
    while True:
        books = {}
        try:
            async with websockets.connect(WS_URL, open_timeout=20, max_size=None) as ws:
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                pinger = asyncio.create_task(send_pings(ws))
                try:
                    async for raw in ws:
                        if raw == "PONG":
                            continue
                        messages = json.loads(raw)
                        for m in messages if isinstance(messages, list) else [messages]:
                            apply_message(m, books, wanted, on_book)
                finally:
                    pinger.cancel()
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            log(f"polymarket stream dropped ({type(e).__name__}), reconnecting")
            await asyncio.sleep(3)


async def stream_books(token_ids, on_book, log=print):
    """
    Keep a live order book for every token and call on_book(token_id, bids, asks)
    after each change. Bids and asks are lists of [price, size], best first.
    Runs forever, reconnecting when a connection drops.
    """
    chunks = [token_ids[i:i + WS_CHUNK] for i in range(0, len(token_ids), WS_CHUNK)]
    await asyncio.gather(*(stream_chunk(chunk, on_book, log) for chunk in chunks))
