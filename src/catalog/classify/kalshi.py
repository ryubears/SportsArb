"""
Turn Kalshi contracts into Bets.

The series ticker says the kind, the event ticker holds the game, and the
market ticker holds the team. College football's and hockey's game series
share the NFL's layout, with team codes of two to five letters in college
football and two or three in hockey. Baseball's event tickers carry the
start time too, which tells a doubleheader's two games apart. Player
props name the player in the title, before the colon. Only games are
read, since only games are traded, so futures are left out. This is the
only file that knows Kalshi's ticker layout.
"""

import re
from catalog.classify.teams import player_key, team_from_code
from collections import defaultdict
from common.timeutil import season_from_date
from datetime import datetime
from db.models import Bet

GAME_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z]+)$")    # '26SEP20CARATL', or '26SEP291400PHIATL' with the Eastern start time.
GAME_SERIES = {
    "KXNFLGAME": "game_winner", "KXNFLSPREAD": "spread", "KXNFLTOTAL": "total",
    "KXNCAAFGAME": "game_winner", "KXNCAAFSPREAD": "spread", "KXNCAAFTOTAL": "total",
    "KXMLBGAME": "game_winner", "KXMLBSPREAD": "spread", "KXMLBTOTAL": "total", "KXMLBTEAMTOTAL": "team_total",
    "KXNHLGAME": "game_winner", "KXNHLSPREAD": "spread", "KXNHLTOTAL": "total", "KXNHLTEAMTOTAL": "team_total",
}
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
    "KXMLBHIT": "player_hits",
    "KXMLBHR": "player_home_runs",
    "KXMLBKS": "player_strikeouts",
    "KXMLBTB": "player_total_bases",
    "KXMLBHRR": "player_hits_runs_rbis",
    "KXMLBRBI": "player_rbis",
    "KXMLBSB": "player_stolen_bases",
    "KXMLBOUTS": "player_outs",
    "KXMLBHA": "player_hits_allowed",
    "KXMLBERA": "player_earned_runs_allowed",
    "KXMLBWA": "player_walks_allowed",
    "KXNHLGOAL": "player_goals",
    "KXNHLPTS": "player_points",
}
PLAYER_TITLE = re.compile(r"^(.+?): ")     # 'Bijan Robinson: 100+ receiving yards'.


def team(code, sport):
    """
    The sport's team a Kalshi ticker code names, or None.
    """
    return team_from_code(code, sport, "kalshi")


def split_codes(pair, sport):
    """
    Split two glued ticker codes such as 'CARATL' or 'WKUNMSU' into two of
    the sport's teams. Codes differ in length, so every split is tried, and
    one that is not the only split into two teams is refused, not guessed.
    """
    splits = [(a, b) for a, b in ((team(pair[:i], sport), team(pair[i:], sport)) for i in range(1, len(pair))) if a and b]
    return splits[0] if len(splits) == 1 else (None, None)


def parse_game(tail, sport):
    """
    Parse a game event tail such as '26SEP20CARATL' or '26SEP291400PHIATL'
    into (date, away, home), the teams being the sport's.
    """
    m = GAME_DATE.match(tail)
    if not m:
        return None, None, None
    yy, mon, dd, _, pair = m.groups()
    date = datetime.strptime(f"20{yy} {mon} {dd}", "%Y %b %d").strftime("%Y-%m-%d")
    away, home = split_codes(pair, sport)
    return date, away, home


def classify(row):
    """
    The Bet a Kalshi contract row describes, or None when it is not one we trade.
    """
    series, event, ticker, sport = row["series_id"], row["event_id"], row["contract_id"], row["sport"]
    event_tail = event[len(series) + 1:]
    market_tail = ticker[len(event) + 1:]
    base = dict(venue=row["venue"], contract_id=row["contract_id"])

    if series in GAME_SERIES:
        game_date, away, home = parse_game(event_tail, sport)
        if not game_date or not (away and home):
            return None
        kind = GAME_SERIES[series]
        common = dict(season=season_from_date(game_date, sport), game_date=game_date, team_a=away, team_b=home, **base)
        if kind == "game_winner":
            # Stated as the away team winning, with the home contract as the complement.
            picked = team(market_tail, sport)
            if picked not in (away, home):
                return None
            return Bet(kind=kind, subject=away, line=None, polarity="yes" if picked == away else "no", **common)
        if kind in ("spread", "team_total"):
            # The market tail is a team code plus a rounded line, for example 'ATL17' for 16.5: the team wins by more,
            # or for a team total, 'ATL2' for 1.5, it scores more.
            subject = team(market_tail.rstrip("0123456789"), sport)
            if not subject or row["line"] is None:
                return None
            return Bet(kind=kind, subject=subject, line=row["line"], polarity="yes", **common)
        if kind == "total":
            return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common) if row["line"] is not None else None

    if series in PLAYER_SERIES:
        game_date, away, home = parse_game(event_tail, sport)
        m = PLAYER_TITLE.match(row["title"] or "")
        if not game_date or not (away and home) or not m or "D/ST" in m.group(1):
            return None
        kind = PLAYER_SERIES[series]
        line = None if kind == "player_first_touchdown" else row["line"]
        if kind != "player_first_touchdown" and line is None:
            return None
        return Bet(kind=kind, season=season_from_date(game_date, sport), game_date=game_date, team_a=away, team_b=home,
                   subject=player_key(m.group(1)), line=line, polarity="yes", **base)
    return None


def doubleheaders(rows):
    """
    The contracts on games the same teams play twice on one date, which a
    bet cannot tell apart yet, since a game is its date and teams. Kalshi's
    baseball event tickers give each game's start time, so a date and teams
    with two start times is a doubleheader.
    """
    starts, games = defaultdict(set), {}
    for row in rows:
        series = row["series_id"]
        if series not in GAME_SERIES and series not in PLAYER_SERIES:
            continue
        m = GAME_DATE.match(row["event_id"][len(series) + 1:])
        if m and m.group(4):
            game = (row["sport"], *m.group(1, 2, 3, 5))
            starts[game].add(m.group(4))
            games[row["contract_id"]] = game
    return {contract_id for contract_id, game in games.items() if len(starts[game]) > 1}
