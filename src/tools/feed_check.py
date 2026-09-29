"""
Compare ways of following the venues' books, side by side on the same markets, without trading.

For a while, several connections to each venue carry the same markets at
once, and each message is timed from the venue's own time for it to its
arrival here:

- Polymarket US: the full book the recorder follows, the same without
  websocket compression, the lite feed of best prices only, and the trade
  feed. The first book of each market on a connection is a snapshot, whose
  time is its last change, so it is left out. The lite feed carries no
  time, so it is only compared with the full book, by when each first
  showed the same best bid and ask.
- Kalshi: the order book on the external-api host the recorder follows,
  which Kalshi recommends, and on the legacy host, each with and without
  compression. Only deltas carry a time.

An update that reached two connections is also compared directly between
them, which sets aside how busy its market was. Then a signed balance
read, the call that keeps the live process's order connections warm, is
timed on each Kalshi REST host.

The markets are those the recorder follows, which leaves out games that
are over, the games nearest in time first, so games under way lead. Run
it during a game to see the feeds under
load. It only listens and reads, so the recorder can keep running. This
process parses every message of every connection, so when it keeps a
core busy its own timing adds to the delays, which it says.

Run from src/ with:
    python3 -m tools.feed_check
    python3 -m tools.feed_check --seconds 300 --markets 100 --sport nfl
"""

import argparse
import asyncio
import json
import statistics
import time
import websockets
from api import kalshi, polymarket_us
from api.http import send_json
from common.stats import quantile
from common.timeutil import epoch
from db.database import read_only
from engine.components.market.record import load_targets

KALSHI_LEGACY_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"      # The host before Kalshi dedicated external-api to API traders.
KALSHI_REST_HOSTS = {"legacy host": "https://api.elections.kalshi.com", "external-api host": "https://external-api.kalshi.com"}
# Each connection's subscription, or url for Kalshi, and its websocket compression: 'deflate', which the library offers by default, or None.
POLYMARKET_US = {
    "full book": ("SUBSCRIPTION_TYPE_MARKET_DATA", "deflate"),
    "full book, uncompressed": ("SUBSCRIPTION_TYPE_MARKET_DATA", None),
    "lite, uncompressed": ("SUBSCRIPTION_TYPE_MARKET_DATA_LITE", None),
    "trades, uncompressed": ("SUBSCRIPTION_TYPE_TRADE", None),
}
KALSHI = {
    "legacy host": (KALSHI_LEGACY_WS, "deflate"),
    "legacy host, uncompressed": (KALSHI_LEGACY_WS, None),
    "external-api host": (kalshi.WS_URL, "deflate"),
    "external-api host, uncompressed": (kalshi.WS_URL, None),
}
# Connections whose shared updates are compared, the second against the first.
COMPARE = [("full book", "full book, uncompressed"), ("full book, uncompressed", "lite, uncompressed"),
           ("full book, uncompressed", "trades, uncompressed"), ("legacy host", "legacy host, uncompressed"),
           ("legacy host", "external-api host"), ("legacy host, uncompressed", "external-api host, uncompressed")]
TIME_KEYS = ("transactTime", "tradeTime", "createTime", "time", "timestamp")    # Where a Polymarket US message may say when.
SUBSCRIPTION_SLUGS = 100    # Polymarket US markets one subscription request may carry.
READS = 10                  # Timed balance reads on each Kalshi REST host.


def find(obj, key):
    """
    The first value under key anywhere in a parsed message, or None.
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        values = obj.values()
    elif isinstance(obj, list):
        values = obj
    else:
        return None
    for value in values:
        found = find(value, key)
        if found is not None:
            return found
    return None


def best(levels):
    """
    The best price of one side of a book as the feed sends it, best first once sorted, or None when the side is empty.
    """
    return levels[0][0] if levels else None


def polymarket_update(m, seen):
    """
    A Polymarket US message as (keys, the venue's time for it or None), or
    None for one without a market, and for the first book of each market,
    its snapshot. The keys name the update the same way on any connection:
    its market and time, and for a book its best bid and ask, which is all
    the lite feed can be compared by.
    """
    slug = find(m, "marketSlug")
    if slug is None:
        return None
    sent = next((t for t in (epoch(find(m, k)) for k in TIME_KEYS) if t is not None), None)
    keys = [(slug, sent)] if sent is not None else []
    if "marketData" in m or "marketDataLite" in m:
        if slug not in seen:
            seen.add(slug)
            return None
        if "marketData" in m:
            data = m["marketData"]
            bid, ask = best(polymarket_us.levels(data.get("bids"), True)), best(polymarket_us.levels(data.get("offers"), False))
        else:
            data = m["marketDataLite"]
            bid, ask = (polymarket_us.amount(data.get(k)) or None for k in ("bestBid", "bestAsk"))
        keys.append(("best", slug, bid, ask))
    return keys, sent


def kalshi_update(m, seen):
    """
    A Kalshi delta as (keys, the matching engine's time for it), or None for any other message.
    """
    body = m.get("msg") or {}
    if m.get("type") != "orderbook_delta" or not body.get("ts_ms"):
        return None
    key = (body.get("market_ticker"), body["ts_ms"], body.get("side"), body.get("price_dollars"), body.get("delta_fp"))
    return [key], body["ts_ms"] / 1000


class Listener:
    """
    One connection's measurements: when each update arrived, and how long after the venue's time for it.
    """

    def __init__(self, label):
        self.label = label
        self.compression = None     # The extension the venue agreed to, or None.
        self.delays = []            # Milliseconds from the venue's time for an update to its arrival, for those with one.
        self.arrived = {}           # Each key of an update maps to when it first arrived.
        self.sizes = []             # Characters in each counted message, once decompressed.
        self.sample = None          # The first counted message, to show what the feed sends.
        self.error = None
        self.seen = set()           # Markets whose snapshot has come, for Polymarket US.

    def count(self, keys, sent, arrived, raw):
        if sent is not None:
            self.delays.append(1000 * (arrived - sent))
        for key in keys:
            self.arrived.setdefault(key, arrived)
        self.sizes.append(len(raw))
        if self.sample is None:
            self.sample = raw[:300]

    def line(self):
        if self.error and not self.sizes:
            return f"  {self.label:34} failed: {self.error}"
        if not self.delays:
            return (f"  {self.label:34} {self.compression or 'no compression':20} {len(self.sizes):6} updates, none with the venue's time"
                    + (f", {statistics.mean(self.sizes) / 1000:.1f} KB a message" if self.sizes else ""))
        return (f"  {self.label:34} {self.compression or 'no compression':20} {len(self.delays):6} updates, "
                f"median {quantile(self.delays, 0.5):5.0f} ms, 10% {quantile(self.delays, 0.1):4.0f}, 90% {quantile(self.delays, 0.9):5.0f}, "
                f"{statistics.mean(self.sizes) / 1000:.1f} KB a message")


def compared(first, second):
    """
    How much sooner the updates both connections carried reached the second, in words.
    """
    shared = first.arrived.keys() & second.arrived.keys()
    if not shared:
        return f"  {second.label} and {first.label} shared no updates"
    sooner = statistics.median(1000 * (first.arrived[k] - second.arrived[k]) for k in shared)
    return f"  {second.label} got the same updates {sooner:+.1f} ms sooner than {first.label} (median of {len(shared)})"


async def listen(listener, url, headers, compression, frames, update, seconds):
    """
    Open one connection, send the subscription frames, and count every update that arrives for the given seconds.
    """
    try:
        async with websockets.connect(url, additional_headers=headers, compression=compression, max_size=None, max_queue=None) as ws:
            listener.compression = ws.response.headers.get("Sec-WebSocket-Extensions")
            for frame in frames:
                await ws.send(json.dumps(frame))
            end = time.monotonic() + seconds
            while (left := end - time.monotonic()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), left)
                except asyncio.TimeoutError:
                    break
                arrived = time.time()
                got = update(json.loads(raw), listener.seen)
                if got:
                    listener.count(*got, arrived, raw)
    except Exception as e:
        listener.error = repr(e)[:200]


def markets(venue, limit, sports):
    """
    The contracts of the sports the recorder follows on the venue, the games nearest in time first, at most limit.
    Kalshi gives no kickoff, so its contracts go by close time, which it sets at the expected final whistle.
    """
    conn = read_only()
    try:
        followed = set(load_targets(conn, sports)[venue])
        when = {cid: epoch(t) for cid, t in conn.execute("SELECT contract_id, COALESCE(start_time, close_time) FROM contracts WHERE venue = ?", (venue,))}
    finally:
        conn.close()
    now = time.time()
    return sorted(followed, key=lambda cid: abs(when[cid] - now) if when.get(cid) else float("inf"))[:limit]


def time_reads(reads=READS):
    """
    The median milliseconds of a signed balance read on each Kalshi REST host, over a kept connection opened by one more read.
    """
    path = kalshi.BASE_PATH + "/portfolio/balance"
    times = {label: [] for label in KALSHI_REST_HOSTS}
    for i in range(reads + 1):
        for label, host in KALSHI_REST_HOSTS.items():
            headers = kalshi.signed_headers("GET", path)
            start = time.perf_counter()
            send_json("GET", host + path, headers)
            if i:
                times[label].append(1000 * (time.perf_counter() - start))
    return {label: statistics.median(ms) for label, ms in times.items()}


async def check(seconds, limit, sports):
    """
    Listen on every connection at once, then print what each measured and how the pairs compare.
    """
    slugs, tickers = markets("polymarket_us", limit, sports), markets("kalshi", limit, sports)
    print(f"listening {seconds:.0f}s to {len(slugs)} polymarket_us and {len(tickers)} kalshi markets on {len(POLYMARKET_US) + len(KALSHI)} connections")
    listeners, jobs = {}, []
    for label, (kind, compression) in POLYMARKET_US.items():
        frames = [{"subscribe": {"requestId": f"check-{i}", "subscriptionType": kind, "marketSlugs": slugs[i:i + SUBSCRIPTION_SLUGS],
                                 "responsesDebounced": False}} for i in range(0, len(slugs), SUBSCRIPTION_SLUGS)]
        listeners[label] = Listener(label)
        jobs.append(listen(listeners[label], polymarket_us.WS_URL, polymarket_us.signed_headers("GET", polymarket_us.WS_PATH),
                           compression, frames, polymarket_update, seconds))
    for label, (url, compression) in KALSHI.items():
        frames = [{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": tickers, "use_yes_price": True}}]
        listeners[label] = Listener(label)
        jobs.append(listen(listeners[label], url, kalshi.signed_headers("GET", kalshi.WS_PATH), compression, frames, kalshi_update, seconds))
    cpu, wall = time.process_time(), time.monotonic()
    await asyncio.gather(*jobs)
    busy = (time.process_time() - cpu) / (time.monotonic() - wall)
    for venue, labels in (("polymarket_us", POLYMARKET_US), ("kalshi", KALSHI)):
        print(f"\n{venue}, from the venue's time to arrival here:")
        for label in labels:
            print(listeners[label].line())
    print("\nthe same updates on two connections:")
    for first, second in COMPARE:
        print(compared(listeners[first], listeners[second]))
    for label in ("lite, uncompressed", "trades, uncompressed"):
        if listeners[label].sample:
            print(f"\na {label} message: {listeners[label].sample}")
    print(f"\nthis process kept {busy:.0%} of a core busy" + (", so its own timing adds to the delays above" if busy > 0.7 else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Compare ways of following the venues' books, without trading.")
    ap.add_argument("--seconds", type=float, default=120, help="how long to listen")
    ap.add_argument("--markets", type=int, default=100, help="markets to follow on each venue, the games nearest in time first")
    ap.add_argument("--sport", default="nfl,ncaaf", help="the sports whose markets to follow, comma separated")
    args = ap.parse_args()
    asyncio.run(check(args.seconds, args.markets, tuple(s.strip() for s in args.sport.split(",") if s.strip())))
    print("\nkalshi REST, a signed balance read on a kept connection, median of "
          f"{READS}: " + ", ".join(f"{label} {ms:.0f} ms" for label, ms in time_reads().items()))
