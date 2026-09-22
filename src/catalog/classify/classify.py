"""
Turn each stored contract into a Bet.

A Bet says what a contract is about in venue neutral terms, so the
matcher can pair contracts across venues by comparing Bet fields
instead of titles. Each venue has its own parser in this folder, and
this file routes rows to them. Contracts
no parser understands are left out and counted.

Run with:
    python3 -m catalog.classify.classify --sport nfl
"""

import argparse
from catalog.classify import kalshi, polymarket_us
from collections import Counter
from db import database

CLASSIFIERS = {"kalshi": kalshi.classify, "polymarket_us": polymarket_us.classify}


def classify_all(rows):
    """
    Classify every contract row. Returns the bets and the rows nobody understood.
    """
    bets, unclassified = [], []
    for row in rows:
        classifier = CLASSIFIERS.get(row["venue"])
        bet = classifier(row) if classifier else None
        if bet:
            bets.append(bet)
        else:
            unclassified.append(row)
    return bets, unclassified


def report(bets, unclassified):
    """
    Print how many contracts landed in each kind, and the biggest unclassified groups.
    """
    counts = Counter((b.venue, b.kind) for b in bets)
    print("classified")
    for (venue, kind), n in sorted(counts.items()):
        print(f"  {venue:10s} {kind:18s} {n:6d}")
    groups = Counter()
    for row in unclassified:
        label = row["series_id"] if row["venue"] == "kalshi" else (row["market_type"] or row["event_title"])
        if row["venue"] not in CLASSIFIERS:
            label = "venue no longer classified"
        groups[(row["venue"], label)] += 1
    print(f"unclassified {len(unclassified)}, largest groups")
    for (venue, label), n in groups.most_common(12):
        print(f"  {venue:10s} {str(label)[:60]:60s} {n:6d}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Classify stored contracts into bets.")
    ap.add_argument("--sport", default="nfl")
    args = ap.parse_args()
    with database.connect() as conn:
        rows = database.load_contracts(conn, sport=args.sport)
        bets, unclassified = classify_all(rows)
        database.replace_bets(conn, args.sport, bets)
    report(bets, unclassified)
