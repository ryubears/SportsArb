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
    Build two lookups from the alias file. Names are matched inside free
    text. Codes are matched only against slug and ticker pieces.
    """
    data = jsonutil.read_file(ALIAS_FILE)
    names, codes = {}, {}
    for team, entry in data.items():
        for n in entry["names"]:
            names[n.lower()] = team
        for c in entry["codes"]:
            codes[c.upper()] = team
    return names, codes


NAME_TO_TEAM, CODE_TO_TEAM = load_aliases()

# Longest names first, so 'Los Angeles Rams' wins over 'Rams' at the same spot.
# The lookarounds stop 'Rams' from matching inside 'Ramsey'.
NAME_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(n) for n in sorted(NAME_TO_TEAM, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def teams_in_text(text):
    """
    Team codes mentioned in the text, in order of first appearance, without repeats.
    """
    found = []
    for m in NAME_PATTERN.finditer(text or ""):
        team = NAME_TO_TEAM[m.group(0).lower()]
        if team not in found:
            found.append(team)
    return found


def team_from_code(piece):
    """
    Canonical team code for a slug or ticker piece, or None.
    """
    return CODE_TO_TEAM.get((piece or "").upper())


def team_from_label(text):
    """
    The one team a short label names, by nickname first and by code second.
    Polymarket labels most outcomes 'Falcons' but a few as 'ATL'. None if unclear.
    """
    teams = teams_in_text(text)
    if len(teams) == 1:
        return teams[0]
    return team_from_code(text.strip()) if not teams else None


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


# POLYMARKET

GAME_SLUG = re.compile(r"^nfl-([a-z]+)-([a-z]+)-\d{4}-\d{2}-\d{2}$")
SPREAD_TITLE = re.compile(r"^Spread: (.+?) \(([+-]?[\d.]+)\)")


def polymarket_game(row):
    """
    Bets for moneyline, spread, and total contracts on a single dated game.
    """
    m = GAME_SLUG.match(row["event_id"])
    if not m or not row["start_time"]:
        return None
    away, home = team_from_code(m.group(1)), team_from_code(m.group(2))
    if not (away and home):
        return None
    game_date = eastern_date(row["start_time"])
    base = dict(venue=row["venue"], contract_id=row["contract_id"], season=season_from_date(game_date),
                game_date=game_date, team_a=away, team_b=home)
    kind, outcome = row["market_type"], row["outcome"]

    if kind == "moneyline":
        picked = team_from_label(outcome)
        if not picked:
            return None
        return Bet(kind="game_winner", subject=picked, line=None, polarity="yes", **base)

    if kind == "spreads":
        # The title names one team with its handicap, for example 'Spread: Falcons (-4.5)'.
        # We restate every spread as 'subject wins by more than line' with a positive line.
        t = SPREAD_TITLE.match(row["title"])
        named, picked = team_from_label(t.group(1)) if t else None, team_from_label(outcome)
        if not named or not picked:
            return None
        handicap = float(t.group(2))
        other = home if named == away else away
        if handicap < 0:
            subject, line = named, -handicap
        else:
            subject, line = other, handicap
        polarity = "yes" if picked == subject else "no"
        return Bet(kind="spread", subject=subject, line=line, polarity=polarity, **base)

    if kind == "totals":
        polarity = {"Over": "yes", "Under": "no"}.get(outcome)
        if row["line"] is None or polarity is None:
            return None
        return Bet(kind="total", subject=None, line=row["line"], polarity=polarity, **base)

    return None


WINS_OUTCOME = re.compile(r"^([OU]) ([\d.]+)$")
WINS_QUESTION = re.compile(r"more than ([\d.]+) (?:wins|games)")


def polymarket_future(row):
    """
    Bets for season long team contracts. The event title says which kind it is.
    """
    event_title, question, outcome = row["event_title"] or "", row["title"], row["outcome"]
    teams = teams_in_text(question)
    if len(teams) != 1:
        return None
    line, polarity = None, "yes"

    if re.match(r"^Pro Football: \d{4} Champion$", event_title):
        kind, season = "champion", season_from_text(event_title)
    elif re.match(r"^Pro Football: \d{4} (AFC|NFC) Champion", event_title):
        kind, season = "conf_champion", season_from_text(event_title)
    elif re.match(r"^Pro Football: (AFC|NFC) (East|West|North|South) Champion", event_title):
        # The question names the year the season starts, so add one.
        kind, season = "division_champion", season_from_text(question) + 1
    elif re.search(r"(AFC|NFC) #1 Seed", event_title):
        kind, season = "conf_top_seed", season_from_text(event_title)
    elif re.search(r"Team to advance to (AFC|NFC) Championship Game", event_title):
        kind, season = "reach_conf_final", season_from_text(question)
    elif "Win Total" in event_title:
        kind, season = "season_wins", season_from_text(question) + 1
        m = WINS_OUTCOME.match(outcome)
        if m:
            line, polarity = float(m.group(2)), "yes" if m.group(1) == "O" else "no"
        else:
            m = WINS_QUESTION.search(question)
            if not m or outcome != "Yes":
                return None
            line = float(m.group(1))
    else:
        return None

    return Bet(venue=row["venue"], contract_id=row["contract_id"], kind=kind, season=season,
                     game_date=None, team_a=None, team_b=None, subject=teams[0], line=line, polarity=polarity)


def classify_polymarket(row):
    """
    Route a Polymarket contract to the game or the futures parser.
    """
    if row["market_type"] in ("moneyline", "spreads", "totals"):
        return polymarket_game(row)
    if row["market_type"] is None:
        return polymarket_future(row)
    return None


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
            subject = team_from_code(market_tail)
            return Bet(kind=kind, subject=subject, line=None, polarity="yes", **common) if subject else None
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


# MAIN

CLASSIFIERS = {"polymarket": classify_polymarket, "kalshi": classify_kalshi}


def classify_all(rows):
    """
    Classify every contract row. Returns the bets and the rows nobody understood.
    """
    bets, unclassified = [], []
    for row in rows:
        bet = CLASSIFIERS[row["venue"]](row)
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
