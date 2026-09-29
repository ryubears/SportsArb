"""
Fetch open sports markets from both venues into SQLite.

Run with:
    python3 -m catalog.fetch --sport nfl
    python3 -m catalog.fetch --sport ncaaf --venue kalshi
"""

import argparse
import time
from api import kalshi, polymarket_us
from common.timeutil import now_iso
from common.venues import VENUES
from db import database

FETCHERS = {"kalshi": kalshi.contracts, "polymarket_us": polymarket_us.contracts}     # Each venue client's catalog call.

# How each of our sport keys maps onto the venues' own categories, as the arguments of each venue's fetcher.
# Kalshi lists hundreds of series for a sport, so only the ones the Kalshi classifier reads are fetched.
# Adding a sport means adding it here, to config.GAME_HOURS, config.DOLLARS_PER_CAP_HOUR, polymarket_us.EVENT_PREFIX, and
# match.KIND_NOTES and match.PLAYER_NOTES, an alias file in classify/aliases/, and its Kalshi series to the Kalshi classifier.
# tests/catalog/test_sports.py fails until the tables agree.
SPORTS = {
    "nfl": {
        "kalshi": {
            "tickers": ["KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL",
                        "KXNFLRECYDS", "KXNFLRSHYDS", "KXNFLPASSYDS", "KXNFLREC", "KXNFLPASSTDS", "KXNFLTD", "KXNFLFIRSTTD",
                        "KXNFLPASSCOMP", "KXNFLPASSATT", "KXNFLPASSINT", "KXNFLRSHATT", "KXNFLRRYDS", "KXNFLLONGREC"],
        },
        "polymarket_us": {
            "tags": ["nfl"],
        },
    },
    "ncaaf": {
        "kalshi": {
            "tickers": ["KXNCAAFGAME", "KXNCAAFSPREAD", "KXNCAAFTOTAL"],
        },
        "polymarket_us": {
            "tags": ["cfb"],
        },
    },
    "mlb": {
        "kalshi": {
            "tickers": ["KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL", "KXMLBTEAMTOTAL",
                        "KXMLBHIT", "KXMLBHR", "KXMLBKS", "KXMLBTB", "KXMLBHRR", "KXMLBRBI", "KXMLBSB", "KXMLBOUTS", "KXMLBHA",
                        "KXMLBERA", "KXMLBWA"],
        },
        "polymarket_us": {
            "tags": ["mlb"],        # Not 'baseball', which brings Korean and Japanese league games too.
        },
    },
    "nhl": {
        "kalshi": {
            "tickers": ["KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL", "KXNHLTEAMTOTAL", "KXNHLGOAL", "KXNHLPTS"],
        },
        "polymarket_us": {
            "tags": ["nhl"],
        },
    },
    "nba": {
        "kalshi": {
            "tickers": ["KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBATEAMTOTAL",
                        "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBABLK"],
        },
        "polymarket_us": {
            "tags": ["nba"],
        },
    },
}


def fetch_contracts(venue, sport):
    """
    Call the right venue client for a sport and return its Contracts.
    """
    return FETCHERS[venue](sport, **SPORTS[sport][venue])


def fetch_and_store(sport, venues, conn, log=print):
    """
    Fetch each venue's catalog, upsert it into the database, and log how
    many contracts came back and how long it took. Returns {venue: contracts}.
    """
    counts = {}
    for venue in venues:
        t0 = time.time()
        contracts = fetch_contracts(venue, sport)
        database.upsert_contracts(conn, contracts, now_iso())
        counts[venue] = len(contracts)
        log(f"fetched {len(contracts):,} {sport} contracts from {venue} in {time.time() - t0:.0f}s")
    return counts


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fetch open sports markets into SQLite.")
    ap.add_argument("--sport", default="nfl", choices=sorted(SPORTS))
    ap.add_argument("--venue", default="all", choices=["all", *VENUES])
    args = ap.parse_args()
    venues = VENUES if args.venue == "all" else [args.venue]
    with database.connect() as conn:
        fetch_and_store(args.sport, venues, conn)
