"""
Polymarket client.

Reads the public metadata API and turns every open sports market into
Contracts. This is the only file that knows Polymarket's field names.
"""

import json
from api.helper import get_json, iso, float_or_none
from models import Contract

GAMMA = "https://gamma-api.polymarket.com"


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
                outcomes = json.loads(m.get("outcomes") or "[]")
                token_ids = json.loads(m.get("clobTokenIds") or "[]")
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
                        raw=m,
                    ))
    return result
