"""
Turn Polymarket US contracts into Bets.

Every contract is a market's long side. The event slug names the game,
and the line is the away team's handicap for spreads. The venue's titles
on positive spread lines contradict its own prices, so only the slug and
the signed line are trusted. Player props name the player in the title
and carry an 'at least N' line, restated as the strict threshold N minus
a half. Only games are read, since only games are traded, so futures are
left out. This is the only file that knows Polymarket US's slug layout.
"""

import re
from catalog.classify.teams import player_key, team_from_code
from common.timeutil import eastern_date, season_from_date
from db.models import Bet

EVENT_PREFIX = {"nfl": "nfl", "ncaaf": "cfb"}     # How each sport's event slugs start.
GAME_EVENTS = {sport: re.compile(rf"^{prefix}-([a-z]+)-([a-z]+)-\d{{4}}-\d{{2}}-\d{{2}}$") for sport, prefix in EVENT_PREFIX.items()}
GAME_KINDS = {
    "football_team_full_game_winner": "game_winner",
    "football_team_full_game_spread": "spread",
    "football_team_full_game_total": "total",
}
PLAYER_KINDS = {
    "football_player_receiving_yards": "player_receiving_yards",
    "football_player_rushing_yards": "player_rushing_yards",
    "football_player_passing_yards": "player_passing_yards",
    "football_player_receptions": "player_receptions",
    "football_player_passing_touchdowns": "player_passing_touchdowns",
    "football_player_touchdowns": "player_touchdowns",
    "football_player_first_touchdown": "player_first_touchdown",
    "football_player_passing_completions": "player_passing_completions",
    "football_player_passing_attempts": "player_passing_attempts",
    "football_player_interceptions_thrown": "player_interceptions_thrown",
    "football_player_rushing_attempts": "player_rushing_attempts",
    "football_player_scrimmage_yards": "player_scrimmage_yards",
    "football_player_longest_reception": "player_longest_reception",
}
PLAYER_TITLE = re.compile(r"^Will (.+?) (?:record|score|throw) ")     # 'Will Bijan Robinson record 40+ receiving yards?'.


def team(code, sport):
    """
    The sport's team a Polymarket US slug code names, or None.
    """
    return team_from_code(code, sport, "polymarket_us")


def classify(row):
    """
    The Bet a Polymarket US contract row describes, or None when it is not one we trade.
    """
    base = dict(venue=row["venue"], contract_id=row["contract_id"])
    sport = row["sport"]
    m = GAME_EVENTS[sport].match(row["event_id"]) if sport in GAME_EVENTS else None
    if m:
        away, home = team(m.group(1), sport), team(m.group(2), sport)
        kind = GAME_KINDS.get(row["market_type"]) or PLAYER_KINDS.get(row["market_type"])
        if not (away and home and kind and row["start_time"]):
            return None
        game_date = eastern_date(row["start_time"])
        common = dict(season=season_from_date(game_date), game_date=game_date, team_a=away, team_b=home, **base)
        if kind in PLAYER_KINDS.values():
            # 'At least N' pays on N or more, which is strictly more than N minus a half.
            name = PLAYER_TITLE.match(row["title"] or "")
            if not name:
                return None
            if kind == "player_first_touchdown":
                return Bet(kind=kind, subject=player_key(name.group(1)), line=None, polarity="yes", **common)
            if row["line"] is None:
                return None
            return Bet(kind=kind, subject=player_key(name.group(1)), line=row["line"] - 0.5, polarity="yes", **common)
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
    return None
