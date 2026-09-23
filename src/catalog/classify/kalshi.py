"""
Turn Kalshi contracts into Bets.

The series ticker says the kind, the event ticker holds the season or the
game, and the market ticker holds the team. Player props name the player
in the title, before the colon. This is the only file that knows Kalshi's
ticker layout.
"""

import re
from catalog.classify.teams import player_key, team_from_code
from common.timeutil import season_from_date
from datetime import datetime
from db.models import Bet

GAME_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$")
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
# Player props on one game. Every one but the first touchdown carries a line, stored as the strict threshold.
PLAYER_SERIES = {
    "KXNFLRECYDS": "player_receiving_yards",
    "KXNFLRSHYDS": "player_rushing_yards",
    "KXNFLPASSYDS": "player_passing_yards",
    "KXNFLREC": "player_receptions",
    "KXNFLPASSTDS": "player_passing_touchdowns",
    "KXNFLTD": "player_touchdowns",
    "KXNFLFIRSTTD": "player_first_touchdown",
    "KXNFLPASSCOMP": "player_passing_completions",
    "KXNFLPASSATT": "player_passing_attempts",
    "KXNFLPASSINT": "player_interceptions_thrown",
    "KXNFLRSHATT": "player_rushing_attempts",
    "KXNFLRRYDS": "player_scrimmage_yards",
    "KXNFLLONGREC": "player_longest_reception",
}
PLAYER_TITLE = re.compile(r"^(.+?): ")     # 'Bijan Robinson: 100+ receiving yards'.
EVENT_TAIL = re.compile(r"^([A-Z]*)(\d{2})([A-Z]*)$")


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


def parse_game(tail):
    """
    Parse a game event tail such as '26SEP20CARATL' into (date, away, home).
    """
    m = GAME_DATE.match(tail)
    if not m:
        return None, None, None
    yy, mon, dd, pair = m.groups()
    date = datetime.strptime(f"20{yy} {mon} {dd}", "%Y %b %d").strftime("%Y-%m-%d")
    away, home = split_codes(pair)
    return date, away, home


def classify(row):
    """
    The Bet a Kalshi contract row describes, or None when it is not one we trade.
    """
    series, event, ticker = row["series_id"], row["event_id"], row["contract_id"]
    event_tail = event[len(series) + 1:]
    market_tail = ticker[len(event) + 1:]
    base = dict(venue=row["venue"], contract_id=row["contract_id"])

    if series in GAME_SERIES:
        game_date, away, home = parse_game(event_tail)
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

    if series in PLAYER_SERIES:
        game_date, away, home = parse_game(event_tail)
        m = PLAYER_TITLE.match(row["title"] or "")
        if not game_date or not (away and home) or not m or "D/ST" in m.group(1):
            return None
        kind = PLAYER_SERIES[series]
        line = None if kind == "player_first_touchdown" else row["line"]
        if kind != "player_first_touchdown" and line is None:
            return None
        return Bet(kind=kind, season=season_from_date(game_date), game_date=game_date, team_a=away, team_b=home,
                   subject=player_key(m.group(1)), line=line, polarity="yes", **base)

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
