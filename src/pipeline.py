"""
Refresh the catalog. Fetch every venue, classify the contracts into bets,
and group them across venues, in one call.

The recorder runs this on a timer so new games enter the bet groups and
fee schedule changes land in the fee history while it is recording. The
same steps are available one at a time as fetch.py, classify.py, and
match.py, which also print their full reports.

Run with:
    python3 src/pipeline.py --sport nfl
"""

import argparse
import classify
import fetch
import match
from common.timeutil import now_iso
from db import database


def refresh(sport, log=print, db_path=None):
    """
    Run fetch, classify, and match for a sport with a fresh database connection.
    Returns a one line summary. Safe to call from a worker thread.
    """
    with database.connect(db_path) as conn:
        parts = []
        for venue in fetch.VENUES:
            contracts = fetch.fetch_contracts(venue, sport)
            fee_records = database.upsert_contracts(conn, contracts, now_iso())
            parts.append(f"{venue} {len(contracts)} contracts, {fee_records} fee records")
            log(f"fetched {parts[-1]}")
        bets, _ = classify.classify_all(database.load_contracts(conn, sport=sport))
        database.replace_bets(conn, sport, bets)
        groups, _ = match.match(database.load_bets(conn, sport))
        database.replace_groups(conn, sport, groups, now_iso())
        parts.append(f"{len(bets)} bets, {len(groups)} groups")
    return ", ".join(parts)


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fetch, classify, and match in one go.")
    ap.add_argument("--sport", default="nfl", choices=sorted(fetch.SPORTS))
    args = ap.parse_args()
    print(refresh(args.sport))
