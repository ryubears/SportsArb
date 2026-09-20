"""
Group bets that describe the same thing into bet groups.

Two bets are the same when kind, season, game date, teams, subject, and
line all agree. Every contract with that identity, on any venue, joins one
BetGroup. The scanner then looks across a group's members for the cheapest
way to hold yes and the cheapest way to hold no.

Run with:
    python3 src/match.py --sport nfl
"""

import argparse
from collections import Counter, defaultdict
from db import database
from db.models import Bet, BetGroup
from util.timeutil import days_between, now_iso

IDENTITY = ("kind", "season", "game_date", "team_a", "team_b", "subject", "line")

# Flag a group when its members stop trading more than this many days apart.
CLOSE_GAP_LIMIT_DAYS = 60

# Known rule differences by kind, from reading the venues' rules text.
# Every group of that kind carries the note so nobody has to reread the rules.
KIND_NOTES = {
    "game_winner": "Ties pay half on both venues. If the game does not start within 48 hours, Kalshi settles at a fair price while Polymarket US waits up to two weeks for a rescheduled game.",
    "spread": "If the game does not start within 48 hours, Kalshi settles at a fair price. Polymarket US waits up to two weeks for a rescheduled game.",
    "total": "If the game does not start within 48 hours, Kalshi settles at a fair price. Polymarket US waits up to two weeks for a rescheduled game.",
}


def identity(bet):
    """
    The fields that make two bets the same bet.
    """
    return tuple(bet[f] for f in IDENTITY)


def label(bet):
    """
    The identity in words, for example 'spread 2026-09-20 CAR@ATL ATL 4.5'.
    """
    parts = [bet["kind"], str(bet["game_date"] or bet["season"])]
    if bet["team_a"]:
        parts.append(f"{bet['team_a']}@{bet['team_b']}")
    if bet["subject"]:
        parts.append(bet["subject"])
    if bet["line"] is not None:
        parts.append(str(bet["line"]))
    return " ".join(parts)


def flags(rows):
    """
    Things a reviewer should check about a group, from its members' close times and kind.
    """
    found = []
    closes = sorted(r["close_time"] for r in rows if r["close_time"])
    if len(closes) > 1 and abs(days_between(closes[0], closes[-1])) > CLOSE_GAP_LIMIT_DAYS:
        found.append(f"close times {days_between(closes[0], closes[-1]):.0f} days apart")
    if rows[0]["kind"] in KIND_NOTES:
        found.append(KIND_NOTES[rows[0]["kind"]])
    return found


def make_group(rows):
    """
    Build a BetGroup from bet rows that share an identity.
    """
    first = rows[0]
    members = [Bet(r["venue"], r["contract_id"], r["kind"], r["season"], r["game_date"], r["team_a"], r["team_b"],
                   r["subject"], r["line"], r["polarity"], label(first)) for r in rows]
    return BetGroup(label=label(first), kind=first["kind"], season=first["season"], game_date=first["game_date"],
                    team_a=first["team_a"], team_b=first["team_b"], subject=first["subject"], line=first["line"],
                    members=members, flags=flags(rows))


def match(bets):
    """
    Group bet rows by identity. Returns the groups that span at least two
    venues, and the bets that were left out because their group has one venue.
    """
    by_identity = defaultdict(list)
    for bet in bets:
        by_identity[identity(bet)].append(bet)
    groups, unmatched = [], []
    for rows in by_identity.values():
        if len({r["venue"] for r in rows}) >= 2:
            groups.append(make_group(rows))
        else:
            unmatched.extend(rows)
    return groups, unmatched


def report(groups, unmatched):
    """
    Print groups per kind and venue set, how many carry a close time flag, and unmatched bets per venue and kind.
    """
    counts = Counter((g.kind, " + ".join(g.venues)) for g in groups)
    flagged = Counter((g.kind, " + ".join(g.venues)) for g in groups if any(f.startswith("close times") for f in g.flags))
    members = Counter()
    for g in groups:
        members[(g.kind, " + ".join(g.venues))] += len(g.members)
    print(f"{'kind':18s} {'venues':40s} {'groups':>6s} {'contracts':>9s} {'close flag':>11s}")
    for key, n in sorted(counts.items()):
        print(f"  {key[0]:16s} {key[1]:40s} {n:6d} {members[key]:9d} {flagged[key]:11d}")
    print(f"total groups {len(groups)} with {sum(len(g.members) for g in groups)} contracts")
    left = Counter((b["venue"], b["kind"]) for b in unmatched)
    print("bets on a single venue only")
    for (venue, kind), n in sorted(left.items()):
        print(f"  {venue:14s} {kind:18s} {n:6d}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Group classified bets across venues.")
    ap.add_argument("--sport", default="nfl")
    args = ap.parse_args()
    with database.connect() as conn:
        bets = database.load_bets(conn, args.sport)
        groups, unmatched = match(bets)
        database.replace_groups(conn, args.sport, groups, now_iso())
    report(groups, unmatched)
