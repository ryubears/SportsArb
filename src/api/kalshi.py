"""
Kalshi client.

Walks sports series to events to markets on the public API and turns
every open market into a Contract. This is the only file that knows
Kalshi's field names.
"""

import time
from api.helper import get_json, iso, float_or_none
from models import Contract

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SLEEP = 0.12   # Seconds between calls, to stay under the public rate limit.


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


def fetch_series(prefixes):
    """
    Sports series whose ticker starts with one of the prefixes.
    """
    all_series = paged("/series", {"category": "Sports", "limit": 200}, "series")
    return [s for s in all_series if s["ticker"].startswith(tuple(prefixes))]


def fetch_events(series_ticker):
    """
    Open events for a series, with their markets nested inside.
    """
    return paged("/events", {
        "series_ticker": series_ticker, "status": "open",
        "with_nested_markets": "true", "limit": 200,
    }, "events")


def contracts(sport, prefixes):
    """
    One Contract per Kalshi market, meaning its Yes side.
    """
    result = []
    for series in fetch_series(prefixes):
        fee_info = {
            "fee_type": series.get("fee_type"),
            "fee_multiplier": series.get("fee_multiplier"),
        }
        for event in fetch_events(series["ticker"]):
            for m in event.get("markets", []):
                if m.get("status") not in (None, "open", "active"):
                    continue
                rules = " ".join(filter(None, [m.get("rules_primary"), m.get("rules_secondary")])) or None
                line = float_or_none(m.get("floor_strike"))
                if line is None:
                    line = float_or_none(m.get("cap_strike"))
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
                    raw=m,
                ))
    return result
