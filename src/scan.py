"""
Find arbitrage episodes in the recorded quotes.

For every bet group the scanner replays its members' quotes in time order.
After each change it finds the cheapest way to hold yes and the cheapest
way to hold no across all members, on any venues, and prices buying both.
An episode is a stretch where that net edge stays above zero after fees.
Each episode is stored as an Opportunity with its two legs, its duration,
its peak edge, how many contracts could have been filled at the peak by
walking the recorded depth, and the return on the capital tied up,
annualized as if the trade were held until the bet pays out.

Run with:
    python3 src/scan.py
    python3 src/scan.py --since 2026-09-19T01:19:41
"""

import argparse
import fees
from bisect import bisect_right
from collections import defaultdict
from db import database
from db.models import Opportunity
from util.timeutil import seconds_between, shift
from venues import SHORT_NAMES

GAME_HOURS = 4          # A game pays out about this long after kickoff.
TARGET_ANNUAL_PCT = 10  # The return an opportunity must beat to be worth the risk.
MAX_QUOTE_AGE = 60      # Seconds. A member whose latest quote is older than this is left out, its book may be stale.
                        # A quote is also left out once its venue's feed has dropped since, whatever its age.


# PRICING

def ladder(quote, polarity, side):
    """
    Cost per contract and size for each level of holding one side of the
    bet through this contract, cheapest first. Holding the side the contract
    pays on means buying it at its asks. Holding the other side means buying
    the opposite outcome, which costs one minus the bid.
    """
    if side == polarity:
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


def cheapest(members, quotes, side, fee_infos):
    """
    The member offering the lowest fee inclusive cost at the top of book to
    hold one side of the bet. Returns (member, cost) or (None, None).
    """
    best, best_cost = None, None
    for m in members:
        levels = ladder(quotes[(m["venue"], m["contract_id"])], m["polarity"], side)
        if not levels:
            continue
        cost = levels[0][0] + fees.fee(m["venue"], levels[0][0], 1, fee_infos[(m["venue"], m["contract_id"])])
        if best_cost is None or cost < best_cost:
            best, best_cost = m, cost
    return best, best_cost


def price_pair(yes, no, quotes, fee_infos):
    """
    Price buying the yes leg and the no leg together. Returns (yes, no, edge at top, size, profit).
    """
    yes_key, no_key = (yes["venue"], yes["contract_id"]), (no["venue"], no["contract_id"])
    edge, size, profit = fill(ladder(quotes[yes_key], yes["polarity"], "yes"), ladder(quotes[no_key], no["polarity"], "no"),
                              yes["venue"], no["venue"], {yes["venue"]: fee_infos[yes_key], no["venue"]: fee_infos[no_key]})
    return yes, no, edge, size, profit


def best_trade(members, quotes, fee_infos):
    """
    The cheapest yes leg and the cheapest no leg across a group's members,
    priced together. The two legs are never the same contract, since buying
    both sides of one book is not a trade between venues and a crossed book
    would look like free money. Returns (yes member, no member, edge at top,
    size, profit), or None when a side has no quotes on another contract.
    """
    yes, _ = cheapest(members, quotes, "yes", fee_infos)
    no, _ = cheapest(members, quotes, "no", fee_infos)
    if yes is None or no is None:
        return None
    if yes is not no:
        return price_pair(yes, no, quotes, fee_infos)
    # One contract is cheapest on both sides. Try the best partner for each side and keep the better pair.
    others = [m for m in members if m is not yes]
    candidates = []
    other_no, _ = cheapest(others, quotes, "no", fee_infos)
    if other_no is not None:
        candidates.append(price_pair(yes, other_no, quotes, fee_infos))
    other_yes, _ = cheapest(others, quotes, "yes", fee_infos)
    if other_yes is not None:
        candidates.append(price_pair(other_yes, no, quotes, fee_infos))
    return max(candidates, key=lambda c: c[2]) if candidates else None


# EPISODES

def trade_words(yes, no):
    """
    The two legs in words, for example 'yes: K buy, no: PM buy other side'.
    """
    def leg(member, side):
        action = "buy" if member["polarity"] == side else "buy other side"
        return f"{side}: {SHORT_NAMES.get(member['venue'], member['venue'])} {action}"
    return f"{leg(yes, 'yes')}, {leg(no, 'no')}"


def resolution_time(start_time, close_time):
    """
    When the bet pays out. Games settle a few hours after kickoff. Futures settle near their close time.
    """
    if start_time:
        return shift(start_time, hours=GAME_HOURS)
    return close_time


def finish(group, peak, start_ts, end_ts):
    """
    Turn an in progress episode into an Opportunity. peak holds the best moment seen so far.
    """
    start_time = next((m["start_time"] for m in group["members"] if m["start_time"]), None)
    live = 1 if start_time and peak["ts"] >= start_time else 0
    # Capital is locked until the slower of the two legs pays, so the later resolution counts.
    pays_at = max((t for t in (resolution_time(leg["start_time"], leg["close_time"]) for leg in (peak["yes"], peak["no"])) if t),
                  default=None)
    days_held = max(seconds_between(peak["ts"], pays_at) / 86400, 1 / 24) if pays_at else None
    return_pct = 100 * peak["edge"] / (1 - peak["edge"])
    return Opportunity(
        label=group["label"],
        kind=group["kind"],
        trade=trade_words(peak["yes"], peak["no"]),
        yes_venue=peak["yes"]["venue"],
        yes_contract=peak["yes"]["contract_id"],
        no_venue=peak["no"]["venue"],
        no_contract=peak["no"]["contract_id"],
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


def unseen_since(gap_starts, quote_ts, now_ts):
    """
    True when a feed drop started after the quote and no later than now, so the quote's book went unseen.
    """
    return bisect_right(gap_starts, now_ts) > bisect_right(gap_starts, quote_ts)


def scan_group(group, quotes_by_contract, fee_histories, gap_starts=None):
    """
    Replay one group's quotes and return its episodes as Opportunities.
    quotes_by_contract and fee_histories are keyed by (venue, contract_id).
    gap_starts maps a venue to the sorted start times of its feed drops.
    """
    gap_starts = gap_starts or {}
    events = sorted((q for key in quotes_by_contract for q in quotes_by_contract[key]), key=lambda q: q.ts)
    latest = {}
    episodes = []
    start_ts, peak = None, None
    for quote in events:
        latest[(quote.venue, quote.contract_id)] = quote
        fee_infos = {key: fee_at(fee_histories[key], quote.ts) for key in latest}
        # A book nobody has updated for a while may be stale, and a book whose venue dropped its
        # connection since the quote went unseen. Neither can be traded against a fresh one.
        fresh = {key for key, q in latest.items()
                 if seconds_between(q.ts, quote.ts) <= MAX_QUOTE_AGE and not unseen_since(gap_starts.get(key[0], []), q.ts, quote.ts)}
        members = [m for m in group["members"] if (m["venue"], m["contract_id"]) in fresh]
        result = best_trade(members, latest, fee_infos) if len(members) >= 2 else None
        if result is None:
            continue
        yes, no, edge, size, profit = result
        if edge > 0:
            if peak is None:
                start_ts = quote.ts
            if peak is None or edge > peak["edge"]:
                peak = {"yes": yes, "no": no, "ts": quote.ts, "edge": edge, "size": size, "profit": profit}
        elif peak is not None:
            episodes.append(finish(group, peak, start_ts, quote.ts))
            start_ts, peak = None, None
    if peak is not None:
        episodes.append(finish(group, peak, start_ts, events[-1].ts))
    return episodes


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


def scan(conn, sport="nfl", since=None):
    """
    Scan every bet group and return its Opportunities.
    """
    groups = database.load_groups(conn, sport)
    ids = defaultdict(set)
    for g in groups.values():
        for m in g["members"]:
            ids[m["venue"]].add(m["contract_id"])
    quotes = {venue: database.load_quotes(conn, venue, wanted, since) for venue, wanted in ids.items()}
    histories = {venue: database.load_fee_history(conn, venue, wanted) for venue, wanted in ids.items()}
    gap_starts = {venue: [gap.start_ts for gap in database.load_gaps(conn, venue, since)] for venue in ids}
    opportunities = []
    for g in groups.values():
        keys = [(m["venue"], m["contract_id"]) for m in g["members"]]
        opportunities.extend(scan_group(g, {k: quotes[k[0]][k[1]] for k in keys}, {k: histories[k[0]][k[1]] for k in keys}, gap_starts))
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
    good = [o for o in opportunities if o.annual_pct is not None and o.annual_pct >= TARGET_ANNUAL_PCT]
    print(f"\nepisodes beating {TARGET_ANNUAL_PCT}% annualized when held to resolution, by profit at peak")
    for o in sorted(good, key=lambda o: -o.peak_profit)[:15]:
        print(f"  {o.label:42s} {o.trade:40s} net {100 * o.peak_edge:4.1f}c x {o.peak_size:6.0f} = {o.peak_profit:7.2f}$ "
              f"return {o.return_pct:4.2f}% over {o.days_held:5.1f}d = {o.annual_pct:7.0f}%/yr, "
              f"lasted {o.seconds:6.0f}s {'live' if o.live else ''}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan recorded quotes for arbitrage episodes.")
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--since", help="only use quotes at or after this ISO timestamp")
    args = ap.parse_args()
    with database.connect() as conn:
        opportunities = scan(conn, args.sport, args.since)
        database.replace_opportunities(conn, opportunities)
    report(opportunities)
