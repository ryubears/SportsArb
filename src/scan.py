"""
Find cross venue arbitrage episodes in the recorded quotes.

For every pair the scanner replays both venues' quotes in time order and,
after each change, prices the two ways of locking in a dollar. An episode
is a stretch where the best net edge stays above zero after fees. Each
episode is stored with its duration, its peak edge, and how many contracts
could have been filled at the peak by walking the recorded depth.

Run with:
    python3 src/scan.py
    python3 src/scan.py --since 2026-09-19T01:19:41
"""

import argparse
import fees
import json
from collections import defaultdict
from datetime import datetime, timedelta
from db import database

GAME_HOURS = 4          # A game pays out about this long after kickoff.
TARGET_ANNUAL_PCT = 10  # The return an opportunity must beat to be worth the risk.

# Each trade names its two legs as a venue and which side of that venue's book to walk.
# Walking 'ask' means buying the contract at its asks.
# Walking 'bid' means buying the opposite outcome, which costs one minus the bid.
# Every trade pays exactly one dollar at settlement because the two legs cover both outcomes.
TRADES = {
    "same": [
        ("buy PM yes, buy K no", ("polymarket", "ask"), ("kalshi", "bid")),
        ("buy K yes, buy PM no", ("kalshi", "ask"), ("polymarket", "bid")),
    ],
    "opposite": [
        ("buy both yes", ("polymarket", "ask"), ("kalshi", "ask")),
        ("buy both no", ("polymarket", "bid"), ("kalshi", "bid")),
    ],
}


def ladder(book, side):
    """
    Cost per contract and size for each level of one leg, cheapest first.
    """
    if side == "ask":
        return [(price, size) for price, size in book["asks"]]
    return [(round(1 - price, 4), size) for price, size in book["bids"]]


def fill(leg_a, leg_b, fee_a, fee_b):
    """
    Walk two ladders together, buying equal amounts of each while the net
    edge per contract stays positive. Returns (edge at the top, contracts, profit).
    """
    i = j = 0
    size = profit = 0.0
    top_edge = None
    while i < len(leg_a) and j < len(leg_b):
        cost_a, size_a = leg_a[i]
        cost_b, size_b = leg_b[j]
        qty = min(size_a, size_b)
        edge = 1 - cost_a - cost_b - fee_a(cost_a, 1) - fee_b(cost_b, 1)
        if top_edge is None:
            top_edge = edge
        if edge <= 0:
            break
        size += qty
        profit += qty * edge
        leg_a[i] = (cost_a, size_a - qty)
        leg_b[j] = (cost_b, size_b - qty)
        if leg_a[i][1] <= 0:
            i += 1
        if leg_b[j][1] <= 0:
            j += 1
    return (top_edge if top_edge is not None else -1.0), size, profit


def best_trade(pair, books, fee_funcs):
    """
    The most profitable trade for a pair given both current books.
    Returns (trade name, edge at top, size, profit).
    """
    relation = "same" if pair["polymarket_polarity"] == pair["kalshi_polarity"] else "opposite"
    best = None
    for name, (venue_a, side_a), (venue_b, side_b) in TRADES[relation]:
        edge, size, profit = fill(ladder(books[venue_a], side_a), ladder(books[venue_b], side_b),
                                  fee_funcs[venue_a], fee_funcs[venue_b])
        if best is None or edge > best[1]:
            best = (name, edge, size, profit)
    return best


def label(pair):
    """
    Short human readable name, for example 'spread 2026-09-20 IND@KC KC 5.5'.
    """
    parts = [pair["kind"], str(pair["game_date"] or pair["season"])]
    if pair["team_a"]:
        parts.append(f"{pair['team_a']}@{pair['team_b']}")
    if pair["subject"]:
        parts.append(pair["subject"])
    if pair["line"] is not None:
        parts.append(str(pair["line"]))
    return " ".join(parts)


def seconds_between(a, b):
    """
    Seconds from ISO timestamp a to ISO timestamp b.
    """
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def resolution_time(start_time, close_time):
    """
    When the bet pays out. Games settle a few hours after kickoff. Futures settle near their close time.
    """
    if start_time:
        return (datetime.fromisoformat(start_time) + timedelta(hours=GAME_HOURS)).isoformat()
    return close_time


def scan_pair(pair, pm_quotes, k_quotes, fee_funcs, start_time, close_time):
    """
    Replay one pair and return its episodes as rows for the opportunities table.
    """
    events = sorted([(ts, "polymarket", b, a) for ts, b, a in pm_quotes] + [(ts, "kalshi", b, a) for ts, b, a in k_quotes])
    books = {}
    episodes = []
    current = None    # [trade, start_ts, peak_ts, peak_edge, peak_size, peak_profit].
    last_ts = None
    for ts, venue, bids, asks in events:
        books[venue] = {"bids": json.loads(bids), "asks": json.loads(asks)}
        last_ts = ts
        if len(books) < 2 or not all(books[v]["bids"] and books[v]["asks"] for v in books):
            continue
        name, edge, size, profit = best_trade(pair, books, fee_funcs)
        if edge > 0:
            if current is None:
                current = [name, ts, ts, edge, size, profit]
            elif edge > current[3]:
                current[0], current[2], current[3], current[4], current[5] = name, ts, edge, size, profit
        elif current is not None:
            episodes.append(finish(pair, current, ts, start_time, close_time))
            current = None
    if current is not None:
        episodes.append(finish(pair, current, last_ts, start_time, close_time))
    return episodes


def finish(pair, current, end_ts, start_time, close_time):
    """
    Turn an in progress episode into an opportunities row.
    """
    trade, start_ts, peak_ts, edge, size, profit = current
    live = 1 if start_time and peak_ts >= start_time else 0
    pays_at = resolution_time(start_time, close_time)
    days_held = max(seconds_between(peak_ts, pays_at) / 86400, 1 / 24) if pays_at else None
    return_pct = 100 * edge / (1 - edge)
    annual_pct = return_pct * 365 / days_held if days_held else None
    return (pair["polymarket_id"], pair["kalshi_id"], pair["kind"], label(pair), trade, start_ts, end_ts,
            seconds_between(start_ts, end_ts), peak_ts, edge, size, profit, live, days_held, return_pct, annual_pct)


def twins_removed(pairs):
    """
    Polymarket lists both outcomes of a spread or total as separate tokens, and
    both pair with the same Kalshi contract. The No token's book mirrors the Yes
    token's, so the two pairs describe one trade. Keep the Yes side only.
    """
    has_yes = {p["kalshi_id"] for p in pairs if p["polymarket_polarity"] == "yes"}
    return [p for p in pairs if p["polymarket_polarity"] == "yes" or p["kalshi_id"] not in has_yes]


def scan(conn, since=None):
    """
    Scan every pair and return the opportunity rows.
    """
    pairs = twins_removed([dict(r) for r in conn.execute("SELECT * FROM pairs")])
    contracts = {(r["venue"], r["contract_id"]): dict(r) for r in conn.execute(
        "SELECT venue, contract_id, fee_info, start_time, close_time FROM contracts")}
    pm_quotes = database.load_quotes(conn, "polymarket", {p["polymarket_id"] for p in pairs}, since)
    k_quotes = database.load_quotes(conn, "kalshi", {p["kalshi_id"] for p in pairs}, since)
    rows = []
    for pair in pairs:
        pm, k = contracts[("polymarket", pair["polymarket_id"])], contracts[("kalshi", pair["kalshi_id"])]
        pm_fee_info, k_fee_info = json.loads(pm["fee_info"] or "{}"), json.loads(k["fee_info"] or "{}")
        fee_funcs = {
            "polymarket": lambda price, n, fi=pm_fee_info: fees.polymarket_fee(price, n, fi),
            "kalshi": lambda price, n, fi=k_fee_info: fees.kalshi_fee(price, n, fi),
        }
        rows.extend(scan_pair(pair, pm_quotes[pair["polymarket_id"]], k_quotes[pair["kalshi_id"]],
                              fee_funcs, pm["start_time"], pm["close_time"]))
    return rows


def report(rows):
    """
    Print episodes by kind and the largest ones.
    """
    by_kind = defaultdict(list)
    for r in rows:
        by_kind[r[2]].append(r)
    print(f"{'kind':18s} {'episodes':>8s} {'live':>5s} {'median s':>9s} {'max edge':>9s} {'max profit':>11s} {'beat target':>12s}")
    for kind, rs in sorted(by_kind.items()):
        secs = sorted(r[7] for r in rs)
        beat = sum(1 for r in rs if r[15] is not None and r[15] >= TARGET_ANNUAL_PCT)
        print(f"  {kind:16s} {len(rs):8d} {sum(r[12] for r in rs):5d} {secs[len(secs) // 2]:9.0f} "
              f"{100 * max(r[9] for r in rs):8.1f}c {max(r[11] for r in rs):10.2f}$ {beat:12d}")
    print(f"total episodes {len(rows)}")
    print(f"\nepisodes beating {TARGET_ANNUAL_PCT}% annualized when held to resolution, by profit at peak")
    good = [r for r in rows if r[15] is not None and r[15] >= TARGET_ANNUAL_PCT]
    for r in sorted(good, key=lambda r: -r[11])[:15]:
        print(f"  {r[3]:42s} {r[4]:22s} net {100 * r[9]:4.1f}c x {r[10]:6.0f} = {r[11]:7.2f}$ "
              f"return {r[14]:4.2f}% over {r[13]:5.1f}d = {r[15]:7.0f}%/yr, lasted {r[7]:6.0f}s {'live' if r[12] else ''}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan recorded quotes for cross venue arbitrage episodes.")
    ap.add_argument("--since", help="only use quotes at or after this ISO timestamp")
    args = ap.parse_args()
    with database.connect() as conn:
        rows = scan(conn, args.since)
        database.replace_opportunities(conn, rows)
    report(rows)
