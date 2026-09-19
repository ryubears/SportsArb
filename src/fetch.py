"""
Fetch open sports markets from both venues into SQLite.

Run with:
    python3 src/fetch.py --sport nfl
    python3 src/fetch.py --sport nfl --venue kalshi
"""

import argparse
import time
from api import kalshi, polymarket
from db import database
from util.timeutil import now_iso

# How each of our sport keys maps onto the venues' own categories.
SPORTS = {
    "nfl": {
        "polymarket_tags": ["nfl"],
        "kalshi_series_prefixes": ["KXNFL"],
        "kalshi_series_tickers": ["KXSB"],   # The Super Bowl winner series does not use the NFL prefix.
    },
}


def fetch_contracts(venue, sport):
    """
    Call the right venue client for a sport and return its Contracts.
    """
    config = SPORTS[sport]
    if venue == "polymarket":
        return polymarket.contracts(sport, config["polymarket_tags"])
    if venue == "kalshi":
        return kalshi.contracts(sport, config["kalshi_series_prefixes"], config["kalshi_series_tickers"])
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
    ap.add_argument("--venue", default="both", choices=["both", "polymarket", "kalshi"])
    args = ap.parse_args()
    venues = ["polymarket", "kalshi"] if args.venue == "both" else [args.venue]
    with database.connect() as conn:
        fetch_and_store(args.sport, venues, conn)
