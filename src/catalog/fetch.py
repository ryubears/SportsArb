"""
Fetch open sports markets from both venues into SQLite.

Run with:
    python3 -m catalog.fetch --sport nfl
    python3 -m catalog.fetch --sport nfl --venue kalshi
"""

import argparse
import time
from api import kalshi, polymarket_us
from common.timeutil import now_iso
from common.venues import VENUES
from db import database

FETCHERS = {"kalshi": kalshi.contracts, "polymarket_us": polymarket_us.contracts}     # Each venue client's catalog call.

# How each of our sport keys maps onto the venues' own categories, as the arguments of each venue's fetcher.
SPORTS = {
    "nfl": {
        "kalshi": {
            "prefixes": ["KXNFL"],
            "tickers": ["KXSB"],     # The Super Bowl winner series does not use the NFL prefix.
        },
        "polymarket_us": {
            "tags": ["nfl"],
        },
    },
}


def fetch_contracts(venue, sport):
    """
    Call the right venue client for a sport and return its Contracts.
    """
    return FETCHERS[venue](sport, **SPORTS[sport][venue])


def fetch_and_store(sport, venues, conn):
    """
    Fetch each venue's catalog and upsert it into the database.
    """
    for venue in venues:
        t0 = time.time()
        contracts = fetch_contracts(venue, sport)
        database.upsert_contracts(conn, contracts, now_iso())
        print(f"{venue:10s} {sport}: {len(contracts):6d} contracts fetched in {time.time() - t0:.0f}s")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fetch open sports markets into SQLite.")
    ap.add_argument("--sport", default="nfl", choices=sorted(SPORTS))
    ap.add_argument("--venue", default="all", choices=["all", *VENUES])
    args = ap.parse_args()
    venues = VENUES if args.venue == "all" else [args.venue]
    with database.connect() as conn:
        fetch_and_store(args.sport, venues, conn)
