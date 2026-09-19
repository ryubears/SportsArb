"""
Find cross venue arbitrage episodes in the recorded quotes.

For every pair the scanner replays both venues' quotes in time order and,
after each change, prices the two ways of locking in a dollar. An episode
is a stretch where the best net edge stays above zero after fees. Each
episode is stored as an Opportunity with its duration, its peak edge, how
many contracts could have been filled at the peak by walking the recorded
depth, and the return on the capital tied up, annualized as if the trade
were held until the bet pays out.

Run with:
    python3 src/scan.py
    python3 src/scan.py --since 2026-09-19T01:19:41
"""

import argparse
import fees
from collections import defaultdict
from db import database
from db.models import Opportunity
from util.timeutil import seconds_between, shift

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


# PRICING

def ladder(quote, side):
    """
    Cost per contract and size for each level of one leg, cheapest first.
    """
    if side == "ask":
        return [(price, size) for price, size in quote.asks]
    return [(round(1 - price, 4), size) for price, size in quote.bids]


def fill(leg_a, leg_b, venue_a, venue_b, fee_infos):
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
        edge = (1 - cost_a - cost_b
                - fees.fee(venue_a, cost_a, 1, fee_infos[venue_a])
                - fees.fee(venue_b, cost_b, 1, fee_infos[venue_b]))
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


def fee_at(history, ts):
    """
    The fee schedule in force at a time, from the contract's fee history.
    Before the first record the first record is used, since it is the
    earliest schedule ever observed. An empty history means no fee.
    """
    current = {}
    for record in history:
        if record.seen_at > ts and current:
            break
        current = record.fee_info
    return current


def best_trade(pair, quotes, fee_infos):
    """
    The most profitable trade for a pair given both venues' current quotes.
    Returns (trade name, edge at top, size, profit).
    """
    relation = "same" if pair["polymarket_polarity"] == pair["kalshi_polarity"] else "opposite"
    best = None
    for name, (venue_a, side_a), (venue_b, side_b) in TRADES[relation]:
        edge, size, profit = fill(ladder(quotes[venue_a], side_a), ladder(quotes[venue_b], side_b),
                                  venue_a, venue_b, fee_infos)
        if best is None or edge > best[1]:
            best = (name, edge, size, profit)
    return best


# EPISODES

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


def resolution_time(start_time, close_time):
    """
    When the bet pays out. Games settle a few hours after kickoff. Futures settle near their close time.
    """
    if start_time:
        return shift(start_time, hours=GAME_HOURS)
    return close_time


def finish(pair, peak, start_ts, end_ts, start_time, close_time):
    """
    Turn an in progress episode into an Opportunity. peak holds the best moment seen so far.
    """
    live = 1 if start_time and peak["ts"] >= start_time else 0
    pays_at = resolution_time(start_time, close_time)
    days_held = max(seconds_between(peak["ts"], pays_at) / 86400, 1 / 24) if pays_at else None
    return_pct = 100 * peak["edge"] / (1 - peak["edge"])
    return Opportunity(
        polymarket_id=pair["polymarket_id"],
        kalshi_id=pair["kalshi_id"],
        kind=pair["kind"],
        label=label(pair),
        trade=peak["trade"],
        start_ts=start_ts,
        end_ts=end_ts,
        seconds=seconds_between(start_ts, end_ts),
        peak_ts=peak["ts"],
        peak_edge=peak["edge"],
        peak_size=peak["size"],
        peak_profit=peak["profit"],
        live=live,
        days_held=days_held,
        return_pct=return_pct,
        annual_pct=return_pct * 365 / days_held if days_held else None,
    )


def scan_pair(pair, pm_quotes, k_quotes, fee_histories, start_time, close_time):
    """
    Replay one pair's quotes and return its episodes as Opportunities.
    fee_histories maps each venue to that contract's list of FeeRecords.
    """
    events = sorted(pm_quotes + k_quotes, key=lambda q: q.ts)
    latest = {}
    episodes = []
    start_ts, peak = None, None
    for quote in events:
        latest[quote.venue] = quote
        if len(latest) < 2 or not all(q.bids and q.asks for q in latest.values()):
            continue
        fee_infos = {venue: fee_at(history, quote.ts) for venue, history in fee_histories.items()}
        trade, edge, size, profit = best_trade(pair, latest, fee_infos)
        if edge > 0:
            if peak is None:
                start_ts, peak = quote.ts, {"trade": trade, "ts": quote.ts, "edge": edge, "size": size, "profit": profit}
            elif edge > peak["edge"]:
                peak = {"trade": trade, "ts": quote.ts, "edge": edge, "size": size, "profit": profit}
        elif peak is not None:
            episodes.append(finish(pair, peak, start_ts, quote.ts, start_time, close_time))
            start_ts, peak = None, None
    if peak is not None:
        episodes.append(finish(pair, peak, start_ts, events[-1].ts, start_time, close_time))
    return episodes


def scan(conn, since=None):
    """
    Scan every pair and return its Opportunities.
    """
    pairs = [dict(r) for r in conn.execute("SELECT * FROM pairs")]
    contracts = {(r["venue"], r["contract_id"]): dict(r) for r in conn.execute(
        "SELECT venue, contract_id, start_time, close_time FROM contracts")}
    pm_ids, k_ids = {p["polymarket_id"] for p in pairs}, {p["kalshi_id"] for p in pairs}
    pm_quotes, k_quotes = database.load_quotes(conn, "polymarket", pm_ids, since), database.load_quotes(conn, "kalshi", k_ids, since)
    pm_fees, k_fees = database.load_fee_history(conn, "polymarket", pm_ids), database.load_fee_history(conn, "kalshi", k_ids)
    opportunities = []
    for pair in pairs:
        pm = contracts[("polymarket", pair["polymarket_id"])]
        fee_histories = {"polymarket": pm_fees[pair["polymarket_id"]], "kalshi": k_fees[pair["kalshi_id"]]}
        opportunities.extend(scan_pair(pair, pm_quotes[pair["polymarket_id"]], k_quotes[pair["kalshi_id"]],
                                       fee_histories, pm["start_time"], pm["close_time"]))
    return opportunities


# REPORT

def report(opportunities):
    """
    Print episodes by kind, then the ones that beat the target return.
    """
    by_kind = defaultdict(list)
    for o in opportunities:
        by_kind[o.kind].append(o)
    print(f"{'kind':18s} {'episodes':>8s} {'live':>5s} {'median s':>9s} {'max edge':>9s} {'max profit':>11s} {'beat target':>12s}")
    for kind, os in sorted(by_kind.items()):
        secs = sorted(o.seconds for o in os)
        beat = sum(1 for o in os if o.annual_pct is not None and o.annual_pct >= TARGET_ANNUAL_PCT)
        print(f"  {kind:16s} {len(os):8d} {sum(o.live for o in os):5d} {secs[len(secs) // 2]:9.0f} "
              f"{100 * max(o.peak_edge for o in os):8.1f}c {max(o.peak_profit for o in os):10.2f}$ {beat:12d}")
    print(f"total episodes {len(opportunities)}")
    print(f"\nepisodes beating {TARGET_ANNUAL_PCT}% annualized when held to resolution, by profit at peak")
    good = [o for o in opportunities if o.annual_pct is not None and o.annual_pct >= TARGET_ANNUAL_PCT]
    for o in sorted(good, key=lambda o: -o.peak_profit)[:15]:
        print(f"  {o.label:42s} {o.trade:22s} net {100 * o.peak_edge:4.1f}c x {o.peak_size:6.0f} = {o.peak_profit:7.2f}$ "
              f"return {o.return_pct:4.2f}% over {o.days_held:5.1f}d = {o.annual_pct:7.0f}%/yr, "
              f"lasted {o.seconds:6.0f}s {'live' if o.live else ''}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan recorded quotes for cross venue arbitrage episodes.")
    ap.add_argument("--since", help="only use quotes at or after this ISO timestamp")
    args = ap.parse_args()
    with database.connect() as conn:
        opportunities = scan(conn, args.since)
        database.replace_opportunities(conn, opportunities)
    report(opportunities)
