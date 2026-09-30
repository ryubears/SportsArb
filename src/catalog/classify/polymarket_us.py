"""
Turn Polymarket US contracts into Bets.

Every contract is a market's long side. The event slug names the game,
and the line is the away team's handicap for spreads, in football,
baseball, hockey, and basketball alike. The venue's titles on positive spread lines
contradict its own prices, so only the slug and the signed line are
trusted. A team total names its team in the market slug. Player props
name the player in the title and carry an 'at least N' line, restated as
the strict threshold N minus a half.

Futures, bets on a season rather than a game, are told apart by the shape
of their event slug, its date written as D: 'nfl-afceast-D-w' is a
division winner. A team future's market slug ends in the team, though not
always in the code the venue's games use: the NFL's champions glue city
and nickname, 'bufbil', and some events use Kalshi's codes, 'gsw'. Its
market title names the team as Kalshi does, 'Ohio St.', which settles
which code it is. An award's market title is the player, and a season win
total's holds its line, 'Atlanta 43+ wins' or '2.5+ Wins'. A future's
season is the one its date falls in. This is the only file that knows
Polymarket US's slug layout.
"""

import math
import re
from catalog.classify.teams import ALIASES, player_key, team_from_code
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

# FUTURES, by the shape of the event slug without its sport's prefix, the date as D. The market slug ends in the team.
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
CONFERENCES = ("aac", "acc", "bigten", "cusa", "mac", "mwc", "pac12", "sbelt")      # Their champions' slugs, 'cfb-accchamp-D-w'.
TEAM_FUTURES = {
    "champ-D": "champion", "champ-D-w": "champion",
    "afcchamp-D-w": "conf_champion", "nfcchamp-D-w": "conf_champion", "alchamp-D": "conf_champion", "nlchamp-D": "conf_champion",
    "eastconf-D-w": "conf_champion", "westconf-D-w": "conf_champion",
    **{f"{c}champ-D-w": "conf_champion" for c in CONFERENCES}, "secchamp-D-winner": "conf_champion", "big12-D-w": "conf_champion",
    **{f"{c}{d}-D-w": "division_champion" for c in ("afc", "nfc") for d in ("east", "north", "south", "west")},
    **{f"{d}div-D-w": "division_champion" for d in ("atl", "cen", "met", "pac")},
    "afc1seed-D": "conf_top_seed", "nfc1seed-D": "conf_top_seed", "eastseed1-D-w": "conf_top_seed", "westseed1-D-w": "conf_top_seed",
    "D-playoffq": "make_playoffs", "playoffs-D-q": "make_playoffs", "cfp-D-playoffq": "make_playoffs",
    "afc-D-champq": "reach_conf_final", "nfc-D-champq": "reach_conf_final", "D-alcsq": "reach_conf_final", "D-nlcsq": "reach_conf_final",
    "eastconf-D-finalq": "reach_conf_final", "westconf-D-finalq": "reach_conf_final",
    "cfp-D-finalq": "reach_final",
    **{f"{c}-D-champq": "reach_conf_title_game" for c in (*CONFERENCES, "big12", "sec")},
    "prestrophy-D-w": "presidents_trophy",
}
SERIES_EVENT = re.compile(r"^(al|nl)wc-([a-z]+)-([a-z]+)-D-w$")     # A wild card series, 'mlb-alwc-bos-nyy-D-w'.
# Awards, whose market title is the player, 'Pete Crow-Armstrong'.
AWARD_FUTURES = {
    "mvp-D-w": "mvp", "opoy-D-w": "offensive_player", "dpoy-D-w": "defensive_player", "oroy-D-w": "offensive_rookie",
    "droy-D-w": "defensive_rookie", "cbpoty-D-w": "comeback_player", "coty-D-w": "coach",
    "al-D-mvp": "al_mvp", "nl-D-mvp": "nl_mvp", "al-D-cy": "al_cy_young", "nl-D-cy": "nl_cy_young", "al-D-roy": "al_rookie",
    "nl-D-roy": "nl_rookie", "ws-D-mvp": "world_series_mvp",
    "hart-D-w": "hart", "norris-D-w": "norris", "vezina-D-w": "vezina", "jackadams-D-w": "jack_adams",
    "D-mostgoals": "goals_leader", "D-mostpts": "points_leader",
    "heisman-D-w": "heisman",
}
# Season totals, whose market title holds the line. One event a team, 'nfl-wins-D-ari', or one for all, the team ending the market slug.
TEAM_LINE_EVENT = re.compile(r"^wins-D-([a-z]+)$")
LINE_FUTURES = {"wins-ou-D": "season_wins", "D-wintotals": "season_wins", "D-teampts": "season_points"}
LINE = re.compile(r"(\d+(?:\.\d+)?)\+")      # '2.5+ Wins' is over 2.5, 'Atlanta 43+ wins' at least 43.


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
    if row["market_type"] == "futures":
        # Before the games, since a future's slug can read like one: 'nfl-wins-ou-2027-01-10'.
        prefixed = sport in EVENT_PREFIX and row["event_id"].startswith(EVENT_PREFIX[sport] + "-")
        return classify_future(row, sport, base) if prefixed else None
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


def letters(text):
    """
    Text reduced to its lower case letters and digits, to compare names however they are spelled out.
    """
    return "".join(ch for ch in text.lower() if ch.isalnum())


def glued_code(full_name):
    """
    The first three letters of a team's city and of its nickname, digits dropped: 'San Francisco 49ers' is 'saners'.
    """
    *city, nickname = full_name.split()
    return letters("".join(city))[:3] + "".join(ch for ch in nickname.lower() if ch.isalpha())[:3]


GLUED_TO_TEAM = {sport: {glued_code(entry["names"][0]): code for code, entry in teams.items()} for sport, teams in ALIASES.items()}


def team_words(code, entry):
    """
    What a title may start with to name a team: its codes and the first word of each of its names.
    """
    codes = entry["codes"] if isinstance(entry["codes"], list) else [c for cs in entry["codes"].values() for c in cs]
    return {letters(w) for w in [code, *codes, *(name.split()[0] for name in entry["names"] if name.split())]}


def named_by(code, sport, title):
    """
    Whether the market title could name the sport's team: it starts with one of the team's codes or name words, or ends
    in its nickname, 'BAL Ravens' or 'LA Chargers'. A title that is only the team, as a future's is, tells apart the
    teams one code could mean.
    """
    words = (title or "").split()
    if not words:
        return True
    entry = ALIASES[sport][code]
    return letters(words[0]) in team_words(code, entry) or letters(words[-1]) == letters(entry["names"][0].split()[-1])


def unique_names(teams):
    """
    Each name only one of the teams goes by, reduced to its letters, mapped to that team.
    """
    owners = defaultdict(set)
    for code, entry in teams.items():
        for name in entry["names"]:
            owners[letters(name)].add(code)
    return {name: codes.pop() for name, codes in owners.items() if len(codes) == 1}


# For the futures whose slug code neither venue uses anywhere else, 'tamu' for Texas A&M.
NAME_TO_TEAM = {sport: unique_names(teams) for sport, teams in ALIASES.items()}


def future_team(row, sport):
    """
    The team a team future's market slug ends in: the code the venue's games use, the glued one, or Kalshi's, the first
    of them whose team the market title could name, and failing those the one team the title is the name of.
    """
    code = row["contract_id"].rsplit("-", 1)[-1]
    for found in (team(code, sport), GLUED_TO_TEAM.get(sport, {}).get(code), team_from_code(code, sport, "kalshi")):
        if found and named_by(found, sport, row["title"]):
            return found
    return NAME_TO_TEAM.get(sport, {}).get(letters(row["title"] or ""))


def strict_line(title):
    """
    The strict threshold a season total's market title states, or None: over 2.5 in '2.5+ Wins' is 2.5, and at least 43 in 'Atlanta 43+ wins' is 42.5.
    """
    m = LINE.search(title or "")
    return math.ceil(float(m.group(1))) - 0.5 if m else None


def classify_future(row, sport, base):
    """
    The Bet a futures contract row describes, or None. Its season is the one the event slug's date falls in.
    """
    event = row["event_id"][len(EVENT_PREFIX[sport]) + 1:]
    date = DATE.search(event)
    if not date:
        return None
    shape = DATE.sub("D", event, count=1)
    future = dict(season=season_from_date(date.group(0), sport), game_date=None, polarity="yes", **base)
    suffix = team(row["contract_id"].rsplit("-", 1)[-1], sport)
    if shape in AWARD_FUTURES:
        name = row["title"]
        return Bet(kind=AWARD_FUTURES[shape], team_a=None, team_b=None, subject=player_key(name), line=None, **future) if name else None
    m = TEAM_LINE_EVENT.match(shape)
    if m or shape in LINE_FUTURES:
        subject, line = (team(m.group(1), sport) if m else suffix), strict_line(row["title"])
        kind = "season_wins" if m else LINE_FUTURES[shape]
        return Bet(kind=kind, team_a=None, team_b=None, subject=subject, line=line, **future) if subject and line is not None else None
    m = SERIES_EVENT.match(shape)
    if m:
        teams = (team(m.group(2), sport), team(m.group(3), sport))
        if suffix not in teams or None in teams:
            return None
        team_a, team_b = sorted(teams)      # The venues list a series' teams in different orders.
        return Bet(kind="wild_card_series", team_a=team_a, team_b=team_b, subject=suffix, line=None, **future)
    subject = future_team(row, sport) if shape in TEAM_FUTURES else None
    if subject:
        return Bet(kind=TEAM_FUTURES[shape], team_a=None, team_b=None, subject=subject, line=None, **future)
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
