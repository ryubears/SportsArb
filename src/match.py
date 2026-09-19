"""
Pair bets across venues.

Two bets are the same when kind, season, game date, teams, subject, and
line all agree. Polarity is kept on the pair so the scanner knows whether
a Yes on one venue lines up with a Yes or a No on the other.

Run with:
    python3 src/match.py --sport nfl
"""

import argparse
from collections import Counter, defaultdict
from db import database
from db.models import Pair
from util.timeutil import days_between, now_iso

IDENTITY = ("kind", "season", "game_date", "team_a", "team_b", "subject", "line")

# Flag a pair when the venues stop trading more than this many days apart.
CLOSE_GAP_LIMIT_DAYS = 60

# Known rule differences by kind, from reading both venues' rules text.
# Every pair of that kind carries the note so nobody has to reread the rules.
KIND_NOTES = {
    "game_winner": "Ties pay half on both venues. If the game does not start within 48 hours, Kalshi settles at a fair price while Polymarket waits for the game.",
    "spread": "If the game does not start within 48 hours, Kalshi settles at a fair price. Polymarket waits, and pays 50-50 only if the game is cancelled.",
    "total": "If the game does not start within 48 hours, Kalshi settles at a fair price. Polymarket waits, and pays 50-50 only if the game is cancelled.",
    "champion": "Polymarket resolves to Other if no champion is crowned by March 31 of the season end year.",
    "season_wins": "Polymarket pays 50-50 if the regular season is cancelled or cut short. Kalshi rules do not say.",
}


def identity(bet):
    """
    The fields that make two bets the same bet.
    """
    return tuple(bet[f] for f in IDENTITY)


def close_gap_days(polymarket_bet, kalshi_bet):
    """
    Kalshi close time minus Polymarket close time, in days. None if either is missing.
    """
    a, b = polymarket_bet["close_time"], kalshi_bet["close_time"]
    if not a or not b:
        return None
    return round(days_between(a, b), 2)


def make_pair(polymarket_bet, kalshi_bet):
    """
    Build a Pair from two bets that share an identity, with flags for a reviewer.
    """
    gap = close_gap_days(polymarket_bet, kalshi_bet)
    flags = []
    if gap is not None and abs(gap) > CLOSE_GAP_LIMIT_DAYS:
        flags.append(f"close times {gap:.0f} days apart")
    if polymarket_bet["kind"] in KIND_NOTES:
        flags.append(KIND_NOTES[polymarket_bet["kind"]])
    return Pair(
        kind=polymarket_bet["kind"],
        season=polymarket_bet["season"],
        game_date=polymarket_bet["game_date"],
        team_a=polymarket_bet["team_a"],
        team_b=polymarket_bet["team_b"],
        subject=polymarket_bet["subject"],
        line=polymarket_bet["line"],
        polymarket_id=polymarket_bet["contract_id"],
        kalshi_id=kalshi_bet["contract_id"],
        polymarket_polarity=polymarket_bet["polarity"],
        kalshi_polarity=kalshi_bet["polarity"],
        close_gap_days=gap,
        flags=flags,
    )


def twins_removed(pairs):
    """
    Polymarket lists both outcomes of a spread or total as separate tokens, and
    both pair with the same Kalshi contract. The No token's book mirrors the Yes
    token's, so the two pairs describe one trade. Keep the Yes side only.
    """
    has_yes = {p.kalshi_id for p in pairs if p.polymarket_polarity == "yes"}
    return [p for p in pairs if p.polymarket_polarity == "yes" or p.kalshi_id not in has_yes]


def match(bets):
    """
    Group bets by identity and pair every Polymarket bet with every Kalshi bet in its group.
    Returns the pairs and the bets that found no partner on the other venue.
    """
    groups = defaultdict(lambda: {"polymarket": [], "kalshi": []})
    for bet in bets:
        groups[identity(bet)][bet["venue"]].append(bet)
    pairs, unmatched = [], []
    for sides in groups.values():
        if sides["polymarket"] and sides["kalshi"]:
            for p in sides["polymarket"]:
                for k in sides["kalshi"]:
                    pairs.append(make_pair(p, k))
        else:
            unmatched.extend(sides["polymarket"] + sides["kalshi"])
    return twins_removed(pairs), unmatched


def report(pairs, unmatched):
    """
    Print pairs per kind, how many carry a close time flag, and unmatched bets per venue and kind.
    """
    counts = Counter(p.kind for p in pairs)
    flagged = Counter(p.kind for p in pairs if any(f.startswith("close times") for f in p.flags))
    opposite = Counter(p.kind for p in pairs if p.polymarket_polarity != p.kalshi_polarity)
    print(f"{'kind':18s} {'pairs':>6s} {'opposite':>9s} {'close flag':>11s}")
    for kind, n in sorted(counts.items()):
        print(f"  {kind:16s} {n:6d} {opposite[kind]:9d} {flagged[kind]:11d}")
    print(f"total pairs {len(pairs)}")
    left = Counter((b["venue"], b["kind"]) for b in unmatched)
    print("unmatched bets")
    for (venue, kind), n in sorted(left.items()):
        print(f"  {venue:10s} {kind:18s} {n:6d}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pair classified bets across venues.")
    ap.add_argument("--sport", default="nfl")
    args = ap.parse_args()
    with database.connect() as conn:
        bets = database.load_bets(conn, args.sport)
        pairs, unmatched = match(bets)
        database.replace_pairs(conn, args.sport, pairs, now_iso())
    report(pairs, unmatched)
