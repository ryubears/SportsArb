"""
Fetch the open futures markets of each sport, and of elections, from both venues into SQLite.

Run with:
    python3 -m catalog.fetch --sport nfl
    python3 -m catalog.fetch --sport ncaaf --venue kalshi
"""

import argparse
import time
from api import kalshi, polymarket_us
from catalog.classify.kalshi import (CONFERENCES, CONTROL_SERIES, HOUSE_RACE_SERIES, NFL_DIVISIONS, NHL_DIVISIONS, SERIES_PATTERNS,
                                    TITLE_FUTURES)
from common.timeutil import now_iso
from common.venues import VENUES
from db import database

FETCHERS = {"kalshi": kalshi.contracts, "polymarket_us": polymarket_us.contracts}     # Each venue client's catalog call.

# How each of our sport keys maps onto the venues' own categories, as the arguments of each venue's fetcher. Only futures
# are cataloged. Kalshi lists thousands of series, so only the ones the Kalshi classifier reads are fetched, by ticker, or
# for elections, which have a series per state, by the shapes it reads. Polymarket US is fetched by tag, and its games left out.
# Adding a sport means adding it here, to polymarket_us.EVENT_PREFIX, an alias file in classify/aliases/, empty when its
# futures name only people, and its Kalshi series to the Kalshi classifier. tests/catalog/test_sports.py fails until the
# tables agree.
SOCCER_LEAGUES = {"epl": ("EPL", "PREMIERLEAGUE", "epl"), "laliga": ("LALIGA", "LALIGA", "lal"), "seriea": ("SERIEA", "SERIEA", "sea"),
                  "bundesliga": ("BUNDESLIGA", "BUNDESLIGA", "bun"), "ligue1": ("LIGUE1", "LIGUE1", "lg1")}
SPORTS = {
    "nfl": {
        "kalshi": {
            "tickers": ["KXSB", "KXNFLAFCCHAMP", "KXNFLNFCCHAMP", "KXNFL1SEED", "KXNFLPLAYOFF", "KXNFLROUNDQUAL", "KXNFLWINS",
                        *(f"KXNFL{d}" for d in NFL_DIVISIONS),
                        "KXNFLMVP", "KXNFLOPOTY", "KXNFLDPOTY", "KXNFLOROTY", "KXNFLDROTY", "KXNFLCPOTY", "KXNFLCOTY",
                        "KXRECORDNFLBEST", "KXNFLLASTTOLOSE", "KXNFLLASTTOWIN",
                        *(f"KXLEADERNFL{s}" for s in ("PYDS", "RYDS", "RUSHYDS", "PTDS", "RTDS", "RUSHTDS", "SACKS", "INT", "PINT")),
                        *(f"KXNFLSEASON{s}" for s in ("PASSYDS", "PASSTDS", "RECYDS", "RECTD", "RSHYDS", "RSHTD", "REC"))],
        },
        "polymarket_us": {"tags": ["nfl"]},
    },
    "ncaaf": {
        "kalshi": {
            "tickers": ["KXNCAAF", "KXNCAAFPLAYOFF", "KXNCAAFFINALIST", "KXNCAAFWINS", "KXHEISMAN", "KXNCAAFSEC", "KXNCAAFSECQ",
                        *(f"KXNCAAF{c}{q}" for c in CONFERENCES for q in ("", "QUAL")),
                        "KXNCAAFUNDEFEATED", "KXNCAAFCONF", *(f"KXNCAAF{c}LEADER" for c in ("SEC", "BIGTEN", "BIG12", "ACC"))],
        },
        "polymarket_us": {"tags": ["cfb"]},
    },
    "mlb": {
        "kalshi": {
            "tickers": ["KXMLB", "KXMLBAL", "KXMLBNL", "KXMLBALCSQUAL", "KXMLBNLCSQUAL", "KXMLBSERIES",
                        "KXMLBALMVP", "KXMLBNLMVP", "KXMLBALCY", "KXMLBNLCY", "KXMLBALROTY", "KXMLBNLROTY", "KXMLBWSMVP", "KXMLBLEADERPLAYOFF"],
        },
        "polymarket_us": {"tags": ["mlb"]},        # Not 'baseball', which brings Korean and Japanese league games too.
    },
    "nhl": {
        "kalshi": {
            "tickers": ["KXNHL", "KXNHLEAST", "KXNHLWEST", *(f"KXNHL{d}" for d in NHL_DIVISIONS),
                        "KXNHLPLAYOFF", "KXNHLPRES", "KXNHLSEASONPTS",
                        "KXNHLHART", "KXNHLNORRIS", "KXNHLVEZINA", "KXNHLADAMS", "KXNHLCALDER", "KXNHLRICHARD", "KXNHLROSS"],
        },
        "polymarket_us": {"tags": ["nhl"]},
    },
    "nba": {
        "kalshi": {"tickers": ["KXNBA", "KXNBAEAST", "KXNBAWEST", "KXNBAEAST1SEED", "KXNBAWEST1SEED", "KXNBAWINS", "KXNBAMVP", "KXNBARECORD"]},
        "polymarket_us": {"tags": ["nba"]},
    },
    "wnba": {"kalshi": {"tickers": ["KXWNBA", "KXWNBAMVP"]}, "polymarket_us": {"tags": ["wnba"]}},
    "ncaab": {"kalshi": {"tickers": ["KXMARMAD"]}, "polymarket_us": {"tags": ["cbb"]}},
    **{sport: {"kalshi": {"tickers": [f"KX{champion}", *(f"KX{short}{s}" for s in ("TOP", "RELEGATION", "LAST", "LEADER"))]
                          + (["KXEPLTEAMPOINTS"] if sport == "epl" else [])},
               "polymarket_us": {"tags": [tag]}}
       for sport, (short, champion, tag) in SOCCER_LEAGUES.items()},
    "ligamx": {"kalshi": {"tickers": ["KXLIGAMX"]}, "polymarket_us": {"tags": ["lmx"]}},
    "mls": {"kalshi": {"tickers": ["KXMLSCUP", "KXMLSEAST", "KXMLSWEST"]}, "polymarket_us": {"tags": ["mls"]}},
    "ucl": {
        "kalshi": {"tickers": ["KXUCL", "KXUCLTOP", "KXUCLTOP8", "KXUCLBOTTOM", "KXUCLROUND", "KXUCLLEADER", "KXBALLONDOR"]},
        "polymarket_us": {"tags": ["ucl"]},       # The Ballon d'Or's event carries this tag.
    },
    "uel": {"kalshi": {"tickers": ["KXUEL"]}, "polymarket_us": {"tags": ["uel"]}},
    "f1": {"kalshi": {"tickers": ["KXF1", "KXF1CONSTRUCTORS"]}, "polymarket_us": {"tags": ["f1"]}},
    "nascar": {"kalshi": {"tickers": ["KXNASCARCUPSERIES", "KXNASCARAUTOPARTSSERIES", "KXNASCARTRUCKSERIES"]}, "polymarket_us": {"tags": ["nascar"]}},
    "ufc": {"kalshi": {"tickers": [t for t in TITLE_FUTURES if t.startswith("KXUFC")]}, "polymarket_us": {"tags": ["ufc"]}},
    "tennis": {"kalshi": {"tickers": ["KXATP1RANK", "KXWTA1RANK"]}, "polymarket_us": {"tags": ["atp", "wta"]}},
    "darts": {"kalshi": {"tickers": ["KXPDCDARTS"]}, "polymarket_us": {"tags": ["pdcdarts"]}},
    "politics": {
        "kalshi": {"tickers": [*CONTROL_SERIES, HOUSE_RACE_SERIES], "patterns": SERIES_PATTERNS},
        "polymarket_us": {"tags": ["politics"]},
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
