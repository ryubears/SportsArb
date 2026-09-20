"""
Fetch open sports markets from both venues into SQLite.

Run with:
    python3 src/fetch.py --sport nfl
    python3 src/fetch.py --sport nfl --venue kalshi
"""

import argparse
import time
from api import kalshi, polymarket_us
from db import database
from util.timeutil import now_iso
from venues import VENUES

# How each of our sport keys maps onto the venues' own categories.
SPORTS = {
    "nfl": {
        "kalshi_series_prefixes": ["KXNFL"],
        "kalshi_series_tickers": ["KXSB"],   # The Super Bowl winner series does not use the NFL prefix.
        "polymarket_us_tags": ["nfl"],
    },
}


def fetch_contracts(venue, sport):
    """
    Call the right venue client for a sport and return its Contracts.
    """
    config = SPORTS[sport]
    if venue == "kalshi":
        return kalshi.contracts(sport, config["kalshi_series_prefixes"], config["kalshi_series_tickers"])
    if venue == "polymarket_us":
        return polymarket_us.contracts(sport, config["polymarket_us_tags"])
    raise ValueError(f"unknown venue {venue}")


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
