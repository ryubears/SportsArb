"""
Turn Polymarket US contracts into Bets.

Every contract is a market's long side. The event slug names the game,
and the line is the away team's handicap for spreads, in football,
baseball, hockey, and basketball alike. The venue's titles on positive spread lines
contradict its own prices, so only the slug and the signed line are
trusted. A team total names its team in the market slug. Player props
name the player in the title and carry an 'at least N' line, restated as
the strict threshold N minus a half. Only games are read, since only
games are traded, so futures are left out. This is the only file that
knows Polymarket US's slug layout.
"""

import re
from catalog.classify.teams import player_key, team_from_code
from collections import defaultdict
from common.timeutil import eastern_date, season_from_date
from db.models import Bet

EVENT_PREFIX = {"nfl": "nfl", "ncaaf": "cfb", "mlb": "mlb", "nhl": "nhl", "nba": "nba"}     # How each sport's event slugs start.
# 'nfl-phi-ten-2026-09-20', the date Eastern, and for a doubleheader's games a suffix, 'mlb-stl-cin-2026-05-23-dh1'.
GAME_EVENTS = {sport: re.compile(rf"^{prefix}-([a-z]+)-([a-z]+)-(\d{{4}}-\d{{2}}-\d{{2}})(-dh\d)?$") for sport, prefix in EVENT_PREFIX.items()}
TEAM_TOTAL = re.compile(r"-tt-([a-z]+)-")   # The team in a team total's market slug, 'tsc-mlb-bos-nyy-2026-09-29-tt-nyy-1pt5'.
GAME_KINDS = {
    "football_team_full_game_winner": "game_winner",
    "football_team_full_game_spread": "spread",
    "football_team_full_game_total": "total",
    "baseball_team_full_game_winner": "game_winner",
    "baseball_team_full_game_spread": "spread",
    "baseball_team_full_game_total": "total",
    "baseball_team_total_runs": "team_total",
    "hockey_team_full_game_winner": "game_winner",
    "hockey_team_full_game_spread": "spread",
    "hockey_team_full_game_total": "total",
    "hockey_team_total_goals": "team_total",
    # Basketball's game markets were typed moneyline, spreads, and totals through June 2026, as every sport's were, and
    # every other sport's are named like these since. Its props were already named as below.
    "basketball_team_full_game_winner": "game_winner",
    "basketball_team_full_game_spread": "spread",
    "basketball_team_full_game_total": "total",
    "basketball_team_total_points": "team_total",
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
    "baseball_player_hits": "player_hits",
    "baseball_player_home_runs": "player_home_runs",
    "baseball_player_strikeouts": "player_strikeouts",
    "baseball_player_total_bases": "player_total_bases",
    "baseball_player_hits_runs_rbis": "player_hits_runs_rbis",
    "baseball_player_rbis": "player_rbis",
    "baseball_player_stolen_bases": "player_stolen_bases",
    "baseball_player_outs": "player_outs",
    "baseball_player_hits_allowed": "player_hits_allowed",
    "baseball_player_earned_runs_allowed": "player_earned_runs_allowed",
    "baseball_player_walks_allowed": "player_walks_allowed",
    "hockey_player_goals": "player_goals",
    "hockey_player_points": "player_points",
    "basketball_player_points": "player_points",
    "basketball_player_rebounds": "player_rebounds",
    "basketball_player_assists": "player_assists",
    "basketball_player_threes": "player_threes",
    "basketball_player_blocks": "player_blocks",
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
        common = dict(season=season_from_date(game_date, sport), game_date=game_date, team_a=away, team_b=home, **base)
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
        if kind == "team_total":
            # Yes pays when the team scores more than the line.
            code = TEAM_TOTAL.search(row["contract_id"])
            subject = team(code.group(1), sport) if code else None
            return Bet(kind=kind, subject=subject, line=row["line"], polarity="yes", **common) if subject in (away, home) else None
        if kind == "spread":
            # Yes pays when the away team covers the line.
            # A negative line means the away team wins by more than it.
            # A positive line means the home team fails to win by more than it.
            if row["line"] < 0:
                return Bet(kind=kind, subject=away, line=-row["line"], polarity="yes", **common)
            return Bet(kind=kind, subject=home, line=row["line"], polarity="no", **common)
        return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common)
    return None


def doubleheaders(rows):
    """
    The contracts on games the same teams play twice on one date, which a
    bet cannot tell apart yet, since a game is its date and teams. Their
    event slugs end in -dh1 and -dh2, though a first game may come without,
    so a date and teams with two events counts too.
    """
    events, games = defaultdict(set), {}
    for row in rows:
        m = GAME_EVENTS[row["sport"]].match(row["event_id"]) if row["sport"] in GAME_EVENTS else None
        if m:
            game = (row["sport"], *m.group(1, 2, 3))
            events[game].add(row["event_id"])
            games[row["contract_id"]] = (game, bool(m.group(4)))
    return {contract_id for contract_id, (game, suffixed) in games.items() if suffixed or len(events[game]) > 1}
