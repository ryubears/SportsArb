"""
Turn Polymarket US contracts into Bets.

Every contract is a market's long side. The event slug names the game or
the future, the market slug ends with the team for futures, and the line
is the away team's handicap for spreads. The venue's titles on positive
spread lines contradict its own prices, so only the slug and the signed
line are trusted. Player props name the player in the title and carry an
'at least N' line, restated as the strict threshold N minus a half. This
is the only file that knows Polymarket US's slug layout.
"""

import re
from catalog.classify.teams import ALIASES, player_key, team_from_code
from common.timeutil import eastern_date, season_from_date
from db.models import Bet

GAME_EVENT = re.compile(r"^nfl-([a-z]+)-([a-z]+)-\d{4}-\d{2}-\d{2}$")
FUTURE_EVENT = re.compile(r"^nfl-([a-z0-9]+)-(\d{4})-\d{2}-\d{2}(?:-w)?$")
QUALIFIER_EVENT = re.compile(r"^nfl-(afc|nfc)-(\d{4})-\d{2}-\d{2}-champq$")
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
FUTURE_KINDS = {
    "champ": "champion",
    "afcchamp": "conf_champion", "nfcchamp": "conf_champion",
    "afc1seed": "conf_top_seed", "nfc1seed": "conf_top_seed",
    "afceast": "division_champion", "afcwest": "division_champion", "afcnorth": "division_champion", "afcsouth": "division_champion",
    "nfceast": "division_champion", "nfcwest": "division_champion", "nfcnorth": "division_champion", "nfcsouth": "division_champion",
}


def glued_code(full_name):
    """
    Some slugs glue the first three letters of the city to the first three
    of the nickname, with digits dropped, so 'San Francisco 49ers' becomes
    'saners' and 'Kansas City Chiefs' becomes 'kanchi'.
    """
    *city, nickname = full_name.split()
    letters = lambda s: "".join(ch for ch in s if ch.isalpha()).lower()
    return letters("".join(city))[:3] + letters(nickname)[:3]


GLUED_TO_TEAM = {glued_code(entry["names"][0]): team for team, entry in ALIASES.items()}


def team_suffix(suffix):
    """
    The team a market slug ends with. Some events use the plain code, others
    the glued form, see glued_code. Three letter codes are tried before two
    letter ones, which is unambiguous.
    """
    if suffix in GLUED_TO_TEAM:
        return GLUED_TO_TEAM[suffix]
    for candidate in (suffix, suffix[:3], suffix[:2]):
        team = team_from_code(candidate)
        if team:
            return team
    return None


def classify(row):
    """
    The Bet a Polymarket US contract row describes, or None when it is not one we trade.
    """
    base = dict(venue=row["venue"], contract_id=row["contract_id"])
    m = GAME_EVENT.match(row["event_id"])
    if m:
        away, home = team_from_code(m.group(1)), team_from_code(m.group(2))
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
    m = FUTURE_EVENT.match(row["event_id"])
    if m and m.group(1) in FUTURE_KINDS:
        kind = FUTURE_KINDS[m.group(1)]
    else:
        m = QUALIFIER_EVENT.match(row["event_id"])
        if not m:
            return None
        kind = "reach_conf_final"
    team = team_suffix(row["contract_id"].rsplit("-", 1)[-1])
    if not team:
        return None
    return Bet(kind=kind, season=int(m.group(2)), game_date=None, team_a=None, team_b=None,
               subject=team, line=None, polarity="yes", **base)
