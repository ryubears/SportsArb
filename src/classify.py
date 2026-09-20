"""
Turn each stored contract into a Bet.

A Bet says what a contract is about in venue neutral terms, so the
matcher can pair contracts across venues by comparing Bet fields
instead of titles. Contracts no parser understands are left out and counted.

Run with:
    python3 src/classify.py --sport nfl
"""

import argparse
import re
from collections import Counter
from datetime import datetime
from db import database
from db.models import Bet
from pathlib import Path
from util import jsonutil
from util.timeutil import eastern_date

ALIAS_FILE = Path(__file__).resolve().parent / "aliases.json"


# TEAMS

def load_aliases():
    """
    Build the code lookup from the alias file. Codes are matched against
    slug and ticker pieces, never inside free text.
    """
    codes = {}
    for team, entry in jsonutil.read_file(ALIAS_FILE).items():
        for c in entry["codes"]:
            codes[c.upper()] = team
    return codes


CODE_TO_TEAM = load_aliases()


def glued_code(full_name):
    """
    Polymarket US glues the first three letters of the city to the first three
    of the nickname in some slugs, with digits dropped, so 'San Francisco 49ers'
    becomes 'saners' and 'Kansas City Chiefs' becomes 'kanchi'.
    """
    *city, nickname = full_name.split()
    letters = lambda s: "".join(ch for ch in s if ch.isalpha()).lower()
    return letters("".join(city))[:3] + letters(nickname)[:3]


GLUED_TO_TEAM = {glued_code(entry["names"][0]): team for team, entry in jsonutil.read_file(ALIAS_FILE).items()}

def team_from_code(piece):
    """
    Canonical team code for a slug or ticker piece, or None.
    """
    return CODE_TO_TEAM.get((piece or "").upper())


def split_codes(pair):
    """
    Split two glued ticker codes such as 'CARATL' or 'GBNYJ' into two teams.
    Three letter codes are tried first because no valid split is ambiguous that way.
    """
    for i in (3, 2):
        a, b = team_from_code(pair[:i]), team_from_code(pair[i:])
        if a and b:
            return a, b
    return None, None


# SEASONS AND DATES

def season_from_text(text):
    """
    Season end year from '2026-27' or from a lone year like '2027'. None if absent.
    """
    m = re.search(r"\b(20\d{2})-(\d{2})\b", text or "")
    if m:
        return int(m.group(1)[:2] + m.group(2))
    m = re.search(r"\b(20\d{2})\b", text or "")
    return int(m.group(1)) if m else None


def season_from_date(game_date):
    """
    Season end year for a game date. Games from August onward belong to the season ending next year.
    """
    year, month = int(game_date[:4]), int(game_date[5:7])
    return year + 1 if month >= 8 else year


# KALSHI

KALSHI_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$")
FUTURE_SERIES = {
    "KXSB": "champion",
    "KXNFLAFCCHAMP": "conf_champion",
    "KXNFLNFCCHAMP": "conf_champion",
    "KXNFL1SEED": "conf_top_seed",
    "KXNFLPLAYOFF": "make_playoffs",
    "KXNFLWINS": "season_wins",
}
DIVISION_SERIES = re.compile(r"^KXNFL(AFC|NFC)(EAST|WEST|NORTH|SOUTH)$")
GAME_SERIES = {"KXNFLGAME": "game_winner", "KXNFLSPREAD": "spread", "KXNFLTOTAL": "total"}
EVENT_TAIL = re.compile(r"^([A-Z]*)(\d{2})([A-Z]*)$")


def kalshi_game(tail):
    """
    Parse a Kalshi game event tail such as '26SEP20CARATL' into (date, away, home).
    """
    m = KALSHI_DATE.match(tail)
    if not m:
        return None, None, None
    yy, mon, dd, pair = m.groups()
    date = datetime.strptime(f"20{yy} {mon} {dd}", "%Y %b %d").strftime("%Y-%m-%d")
    away, home = split_codes(pair)
    return date, away, home


def classify_kalshi(row):
    """
    Bets for Kalshi contracts. The series ticker says the kind, the event
    ticker holds the season or the game, and the market ticker holds the team.
    """
    series, event, ticker = row["series_id"], row["event_id"], row["contract_id"]
    event_tail = event[len(series) + 1:]
    market_tail = ticker[len(event) + 1:]
    base = dict(venue=row["venue"], contract_id=row["contract_id"])

    if series in GAME_SERIES:
        game_date, away, home = kalshi_game(event_tail)
        if not game_date or not (away and home):
            return None
        kind = GAME_SERIES[series]
        common = dict(season=season_from_date(game_date), game_date=game_date, team_a=away, team_b=home, **base)
        if kind == "game_winner":
            # Stated as the away team winning, with the home contract as the complement.
            picked = team_from_code(market_tail)
            if picked not in (away, home):
                return None
            return Bet(kind=kind, subject=away, line=None, polarity="yes" if picked == away else "no", **common)
        if kind == "spread":
            # The market tail is a team code plus a rounded line, for example 'ATL17' for 16.5.
            subject = team_from_code(market_tail.rstrip("0123456789"))
            if not subject or row["line"] is None:
                return None
            return Bet(kind=kind, subject=subject, line=row["line"], polarity="yes", **common)
        if kind == "total":
            return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common) if row["line"] is not None else None

    kind = FUTURE_SERIES.get(series) or ("division_champion" if DIVISION_SERIES.match(series) else None)
    if not kind:
        return None
    m = EVENT_TAIL.match(event_tail)
    if not m:
        return None
    prefix, yy, suffix = m.groups()
    season = 2000 + int(yy)
    if series == "KXNFL1SEED":
        # This series names the year the season starts, for example 'AFC26'.
        season += 1
    subject = team_from_code(suffix) or team_from_code(market_tail.rstrip("0123456789"))
    if not subject:
        return None
    line = row["line"] if kind == "season_wins" else None
    if kind == "season_wins" and line is None:
        return None
    return Bet(kind=kind, season=season, game_date=None, team_a=None, team_b=None,
                     subject=subject, line=line, polarity="yes", **base)


# POLYMARKET US

US_GAME_EVENT = re.compile(r"^nfl-([a-z]+)-([a-z]+)-\d{4}-\d{2}-\d{2}$")
US_FUTURE_EVENT = re.compile(r"^nfl-([a-z0-9]+)-(\d{4})-\d{2}-\d{2}(?:-w)?$")
US_QUALIFIER_EVENT = re.compile(r"^nfl-(afc|nfc)-(\d{4})-\d{2}-\d{2}-champq$")
US_GAME_KINDS = {
    "football_team_full_game_winner": "game_winner",
    "football_team_full_game_spread": "spread",
    "football_team_full_game_total": "total",
}
US_FUTURE_KINDS = {
    "champ": "champion",
    "afcchamp": "conf_champion", "nfcchamp": "conf_champion",
    "afc1seed": "conf_top_seed", "nfc1seed": "conf_top_seed",
    "afceast": "division_champion", "afcwest": "division_champion", "afcnorth": "division_champion", "afcsouth": "division_champion",
    "nfceast": "division_champion", "nfcwest": "division_champion", "nfcnorth": "division_champion", "nfcsouth": "division_champion",
}


def classify_polymarket_us(row):
    """
    Bets for Polymarket US contracts. Every contract is a market's long side.
    The event slug names the game or the future, the market slug ends with
    the team for futures, and the line is the away team's handicap for spreads.
    The venue's titles on positive spread lines contradict its own prices, so
    only the slug and the signed line are trusted.
    """
    base = dict(venue=row["venue"], contract_id=row["contract_id"])
    m = US_GAME_EVENT.match(row["event_id"])
    if m:
        away, home = team_from_code(m.group(1)), team_from_code(m.group(2))
        kind = US_GAME_KINDS.get(row["market_type"])
        if not (away and home and kind and row["start_time"]):
            return None
        game_date = eastern_date(row["start_time"])
        common = dict(season=season_from_date(game_date), game_date=game_date, team_a=away, team_b=home, **base)
        if kind == "game_winner":
            return Bet(kind=kind, subject=away, line=None, polarity="yes", **common)
        if row["line"] is None:
            return None
        if kind == "spread":
            # Yes pays when the away team covers the line.
            # A negative line means the away team wins by more than it.
            # A positive line means the home team fails to win by more than it.
            if row["line"] < 0:
                return Bet(kind=kind, subject=away, line=-row["line"], polarity="yes", **common)
            return Bet(kind=kind, subject=home, line=row["line"], polarity="no", **common)
        return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common)
    m = US_FUTURE_EVENT.match(row["event_id"])
    if m and m.group(1) in US_FUTURE_KINDS:
        kind = US_FUTURE_KINDS[m.group(1)]
    else:
        m = US_QUALIFIER_EVENT.match(row["event_id"])
        if not m:
            return None
        kind = "reach_conf_final"
    team = us_team_suffix(row["contract_id"].rsplit("-", 1)[-1])
    if not team:
        return None
    return Bet(kind=kind, season=int(m.group(2)), game_date=None, team_a=None, team_b=None,
               subject=team, line=None, polarity="yes", **base)


def us_team_suffix(suffix):
    """
    The team a Polymarket US market slug ends with. Some events use the plain
    code, others glue city and nickname letters together, see glued_code.
    Three letter codes are tried before two letter ones, which is unambiguous.
    """
    if suffix in GLUED_TO_TEAM:
        return GLUED_TO_TEAM[suffix]
    for candidate in (suffix, suffix[:3], suffix[:2]):
        team = team_from_code(candidate)
        if team:
            return team
    return None


# MAIN

CLASSIFIERS = {"kalshi": classify_kalshi, "polymarket_us": classify_polymarket_us}


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


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Classify stored contracts into bets.")
    ap.add_argument("--sport", default="nfl")
    args = ap.parse_args()
    with database.connect() as conn:
        rows = database.load_contracts(conn, sport=args.sport)
        bets, unclassified = classify_all(rows)
        database.replace_bets(conn, args.sport, bets)
    report(bets, unclassified)
