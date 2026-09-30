"""
Pair up the bets that describe the same thing on both venues.

Two bets are the same when kind, season, game date, teams, subject, and
line all agree. Every contract with that identity joins one Pair, which
only exists when both venues list the bet. The scanner then looks across
a pair's members for the cheapest way to hold yes and the cheapest way
to hold no.

Run with:
    python3 -m catalog.match --sport nfl
"""

import argparse
from collections import Counter, defaultdict
from catalog import notes
from common.timeutil import days_between, now_iso
from db import database
from db.models import Bet, Pair

IDENTITY = ("kind", "season", "game_date", "team_a", "team_b", "subject", "line")

# Flag a pair when its members stop trading more than this many days apart.
CLOSE_GAP_LIMIT_DAYS = 60


def identity(bet):
    """
    The fields that make two bets the same bet.
    """
    return tuple(bet[f] for f in IDENTITY)


def label(bet, sport):
    """
    The sport and the identity in words, for example 'nfl spread 2026-09-20
    CAR@ATL ATL 4.5', or for a future the season in place of the game,
    'nfl champion 2027 KC'. The sport keeps it unique across sports, whose
    bets can read the same: the Broncos' and the Nuggets' titles are both
    'champion 2027 DEN'.
    """
    parts = [sport, bet["kind"], bet["game_date"] or str(bet["season"])]
    if bet["team_a"]:
        parts.append(f"{bet['team_a']}@{bet['team_b']}" if bet["game_date"] else f"{bet['team_a']} vs {bet['team_b']}")
    if bet["subject"]:
        parts.append(bet["subject"])
    if bet["line"] is not None:
        parts.append(str(bet["line"]))
    return " ".join(parts)


def flags(rows, sport):
    """
    Things a reviewer should check about a pair of the sport: its members'
    close times far apart, and where the venues' rules on its kind differ,
    see notes.py.
    """
    found = []
    closes = sorted(r["close_time"] for r in rows if r["close_time"])
    if len(closes) > 1 and abs(days_between(closes[0], closes[-1])) > CLOSE_GAP_LIMIT_DAYS:
        found.append(f"close times {days_between(closes[0], closes[-1]):.0f} days apart")
    return found + notes.for_pair(sport, rows[0]["kind"])


def make_pair(rows, sport):
    """
    Build a Pair of the sport from bet rows that share an identity.
    """
    first = rows[0]
    members = [Bet(r["venue"], r["contract_id"], r["kind"], r["season"], r["game_date"], r["team_a"], r["team_b"],
                   r["subject"], r["line"], r["polarity"]) for r in rows]
    return Pair(sport=sport, label=label(first, sport), kind=first["kind"], season=first["season"], game_date=first["game_date"],
                team_a=first["team_a"], team_b=first["team_b"], subject=first["subject"], line=first["line"],
                members=members, flags=flags(rows, sport))


def match(bets, sport):
    """
    Pair up one sport's bet rows by identity. Returns the pairs both venues
    list, and the bets that were left out because only one venue lists them.
    """
    by_identity = defaultdict(list)
    for bet in bets:
        by_identity[identity(bet)].append(bet)
    pairs, unmatched = [], []
    for rows in by_identity.values():
        if len({r["venue"] for r in rows}) >= 2:
            pairs.append(make_pair(rows, sport))
        else:
            unmatched.extend(rows)
    return pairs, unmatched


def report(pairs, unmatched):
    """
    Print pairs per kind and venue set, how many carry a close time flag, and unmatched bets per venue and kind.
    """
    counts = Counter((p.kind, " + ".join(p.venues)) for p in pairs)
    flagged = Counter((p.kind, " + ".join(p.venues)) for p in pairs if any(f.startswith("close times") for f in p.flags))
    members = Counter()
    for p in pairs:
        members[(p.kind, " + ".join(p.venues))] += len(p.members)
    print(f"{'kind':18s} {'venues':40s} {'pairs':>6s} {'contracts':>9s} {'close flag':>11s}")
    for key, n in sorted(counts.items()):
        print(f"  {key[0]:16s} {key[1]:40s} {n:6d} {members[key]:9d} {flagged[key]:11d}")
    print(f"total pairs {len(pairs)} with {sum(len(p.members) for p in pairs)} contracts")
    left = Counter((b["venue"], b["kind"]) for b in unmatched)
    print("bets on a single venue only")
    for (venue, kind), n in sorted(left.items()):
        print(f"  {venue:14s} {kind:18s} {n:6d}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pair classified bets across venues.")
    ap.add_argument("--sport", default="nfl")
    args = ap.parse_args()
    with database.connect() as conn:
        bets = database.load_bets(conn, args.sport)
        pairs, unmatched = match(bets, args.sport)
        database.replace_pairs(conn, args.sport, pairs, now_iso())
    report(pairs, unmatched)
