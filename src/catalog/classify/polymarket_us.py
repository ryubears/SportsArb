"""
Turn Polymarket US contracts into Bets.

Every contract is a market's long side. Games, matches, races, and
Bitcoin's windows are bets on one event, which paper trades: the event
slug names both sides and the date, the market's type its kind, and its
slug or line whom it is on, see the tables below. Their sides are kept as
the Kalshi classifier keeps them, so the same bet reads alike on both.

Futures are bets on a season, a title, an award, a season's leader, a price
by a deadline, or an election, which live trades. Futures are told apart by
the shape of their event slug, with its sport's prefix taken off and its
date written as D: 'nfl-afceast-D-w' is a division winner. A shape is
looked up with its prefix first, 'ucl-D-lastplace', where two sports share
one that means different things, then without it. A team future's market
slug ends in the team, though not always in the code the venue's games
used: the NFL's champions glue city and nickname, 'bufbil', and some events
use Kalshi's codes, 'gsw'. Its market title names the team, 'Ohio St.',
which settles which code it is. An award's, a leader's, or a title holder's
market title is the person, and a season total's holds its line, 'Atlanta
43+ wins', '2.5+ Wins', or '3,750.5+ Passing Yards', unless the shape does,
'nhl-pts100-D'. A future's season is the one its date falls in, or its
event's end when that is half a year later, since a slug can carry the
wrong year: the Champions League's final is 'ucl-final-2026-06-05-w' for
2027's.

Elections, 'usse-ga-2026-11-03', have a market per party, its slug ending
in -dem or -rep, or per candidate, and a race's season is its election
year. In a state whose general election can put two of one party on the
ballot, see teams.TOP_TWO_STATES, only the candidates are paired. This is
the only file that knows Polymarket US's slug layout.
"""

import math
import re
from catalog.classify.teams import (ALIASES, STATES, TOP_TWO_STATES, match_sides, person, player_key, race, side_key, team_from_code,
                                    venue_codes)
from collections import defaultdict
from common.sports import MATCH_SPORTS, RACING, SOCCER, TEAM_SPORTS
from common.timeutil import days_between, eastern_date, last_day, season_from_date, written_date
from common.venues import VENUES
from db.models import Bet

# How each sport's event slugs start. A sport can have several, the Champions League's and the Ballon d'Or's. Bitcoin's
# have none, 'btc-updown-15m-2026-10-04-0530z'.
EVENT_PREFIX = {
    "nfl": ("nfl",), "ncaaf": ("cfb",), "mlb": ("mlb",), "nhl": ("nhl",), "nba": ("nba",), "wnba": ("wnba",), "ncaab": ("cbb",),
    "epl": ("epl",), "laliga": ("lal",), "seriea": ("sea",), "bundesliga": ("bun",), "ligue1": ("lg1",), "ligamx": ("lmx",),
    "mls": ("mls",), "ucl": ("ucl", "uefa"), "uel": ("uel",),
    "f1": ("f1",), "nascar": ("nascar",), "ufc": ("ufc",), "tennis": ("atp", "wta"), "darts": ("pdc", "pdcdarts"),
    "politics": ("usho", "usse", "usgub", "ushr"), "crypto": (),
}

# GAMES, MATCHES, AND RACES, whose event slug names both sides and the date, 'nfl-phi-ten-2026-09-20' or
# 'atp-valvac-artfil-2026-10-03', a doubleheader's game with a suffix, 'mlb-stl-cin-2026-05-23-dh1'. A market's type says
# its kind. Every market is its long side: a winner's is the first side named, and a spread's is the first side covering
# its signed line, so a negative line is the first side winning by more than it, and a positive one the second side not
# winning by more than it. The venue's titles on positive lines contradict its prices, so only the slug and the line are
# trusted. A game of teams keeps them away then home, the first side then the second, as Kalshi does.
GAME_EVENT = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})(-dh\d)?$")
TEAM_TOTAL = re.compile(r"-tt-([a-z]+)-")   # The team in a team total's market slug, 'tsc-mlb-bos-nyy-2026-09-29-tt-nyy-1pt5'.
GAME_KINDS = {
    **{f"{sport}_team_full_game_{bet}": kind for sport in ("football", "baseball", "hockey", "basketball")
       for bet, kind in (("winner", "game_winner"), ("spread", "spread"), ("total", "total"))},
    "baseball_team_total_runs": "team_total", "hockey_team_total_goals": "team_total", "basketball_team_total_points": "team_total",
}
PLAYER_KINDS = {
    **{f"football_player_{stat}": f"player_{stat}" for stat in (
        "receiving_yards", "rushing_yards", "passing_yards", "receptions", "passing_touchdowns", "touchdowns", "first_touchdown",
        "passing_completions", "passing_attempts", "interceptions_thrown", "rushing_attempts", "scrimmage_yards", "longest_reception")},
    **{f"baseball_player_{stat}": f"player_{stat}" for stat in (
        "hits", "home_runs", "strikeouts", "total_bases", "hits_runs_rbis", "rbis", "stolen_bases", "outs", "hits_allowed",
        "earned_runs_allowed", "walks_allowed")},
    "hockey_player_goals": "player_goals", "hockey_player_points": "player_points",
    **{f"basketball_player_{stat}": f"player_{stat}" for stat in ("points", "rebounds", "assists", "threes", "blocks")},
}
PLAYER_TITLE = re.compile(r"^Will (.+?) (?:record|score|throw) ")     # 'Will Bijan Robinson record 40+ receiving yards?'.
SOCCER_KINDS = {
    "soccer_team_full_time_winner": "result", "soccer_team_first_half_winner": "first_half_result",
    "soccer_team_second_half_winner": "second_half_result",
    "soccer_team_full_game_spread": "spread", "soccer_team_first_half_spread": "first_half_spread",
    "soccer_team_second_half_spread": "second_half_spread",
    "soccer_team_full_game_total": "total", "soccer_team_first_half_total": "first_half_total",
    "soccer_team_second_half_total": "second_half_total",
    "soccer_game_btts": "btts", "soccer_game_first_half_btts": "first_half_btts", "soccer_game_second_half_btts": "second_half_btts",
    "soccer_game_exact_score": "exact_score", "soccer_game_first_half_exact_score": "first_half_exact_score",
    "soccer_game_total_corners": "total_corners",
}
SCORE_SLUG = re.compile(r"-exact-score-(\d+)-(\d+)$")      # The first club's goals, then the second's.
CORNERS_SLUG = re.compile(r"-cor-all-")                     # Both clubs' corners, not one's.
MATCH_KINDS = {
    "tennis_match_winner": "match_winner", "darts_match_winner": "match_winner", "ufc_fight_winner": "match_winner",
    "tennis_match_games_spread": "games_spread", "tennis_match_sets_spread": "sets_spread",
    "tennis_match_total_games": "total_games", "tennis_match_total_sets": "total_sets",
    **{f"tennis_set_{n}_winner": "set_winner" for n in range(1, 6)},
    "tennis_match_exact_score": "exact_score", "ufc_go_the_distance": "go_the_distance", "ufc_round_of_victory": "round_of_victory",
}
SET_TITLE = re.compile(r"^Will (.+?) win set (\d)")         # 'Will Valentin Vacherot win set 1 against Arthur Fils?'.
SETS_SLUG = re.compile(r"-es-(\d)-(\d)$")                   # The first player's sets, then the second's.
ROUND_SLUG = re.compile(r"-rov-f([12])-r(\d)$")             # The first or second fighter winning in a round.
DISTANCE_SLUG = re.compile(r"-gtd-(yes|no)$")
RACING_EVENT = re.compile(r"^(f1|nascar)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})-(w|cons)$")     # 'f1-gabgpim-2026-10-04-w', a race's winner.

# BITCOIN. A window's direction, 'btc-updown-15m-2026-10-04-0530z' its start in UTC, Yes being up. A price future's rules
# give its strike and when it ends, 'above $149,999.99 at any point before 12:00 AM ET on January 1, 2027', and a band's
# title its range, '145,000 to 149,999.99'.
UPDOWN_EVENT = re.compile(r"^btc-updown-(15m|1h)-(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})z$")
PRICE_RULE = re.compile(r"\b(above|below) (?:\$([\d,]+(?:\.\d+)?)|the price specified in the title)")
RULE_END = re.compile(r"(\d{1,2}):(\d{2}) ?([AP]M) ET on ([A-Z][a-z]+ \d{1,2}, \d{4})")
PRICE_TITLE = re.compile(r"\$([\d,]+(?:\.\d+)?)")
RANGE_EVENT = "btc-pricerange-yr-"

# TEAM FUTURES, by the shape of the event slug. The market slug ends in the team.
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
CONFERENCES = ("aac", "acc", "bigten", "cusa", "mac", "mwc", "pac12", "sbelt")      # Their champions' slugs, 'cfb-accchamp-D-w'.
TEAM_FUTURES = {
    "champ-D": "champion", "champ-D-w": "champion", "title-D-w": "champion", "final-D-w": "champion", "winner-D": "champion",
    "champion-D-w": "champion",
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
    "bestrecord-D": "best_record", "D-bestrecord": "best_record", "D-lastundefeated": "last_undefeated", "D-lastwinless": "last_winless",
    "undefeated-D": "undefeated",
    "D-top2": "top_2", "D-top4": "top_4", "D-top6": "top_6", "D-relegation": "relegated", "D-lastplace": "last_place",
    "apertura-D-w": "apertura_champion", "clausura-D-w": "clausura_champion",
    "ucl-D-lgphasetop": "league_phase_top", "ucl-D-top8": "league_phase_top_8", "ucl-D-lastplace": "league_phase_last",
    "ucl-D-finalq": "reach_final",
    "cc-D-w": "constructors_champion",
}
SERIES_EVENT = re.compile(r"^(al|nl)wc-([a-z]+)-([a-z]+)-D-w$")     # A wild card series, 'mlb-alwc-bos-nyy-D-w'.
# The conference that wins college football's title, 'cfb-conf-D-w', the market slug ending in the conference.
CONFERENCE_EVENT = "conf-D-w"
CONFERENCE_CODES = {"bigten": "big_ten", "sec": "sec", "big12": "big_12", "acc": "acc"}

# PERSON FUTURES, whose market title is the person, 'Pete Crow-Armstrong'.
AWARD_FUTURES = {
    "mvp-D-w": "mvp", "opoy-D-w": "offensive_player", "dpoy-D-w": "defensive_player", "oroy-D-w": "offensive_rookie",
    "droy-D-w": "defensive_rookie", "cbpoty-D-w": "comeback_player", "coty-D-w": "coach",
    "al-D-mvp": "al_mvp", "nl-D-mvp": "nl_mvp", "al-D-cy": "al_cy_young", "nl-D-cy": "nl_cy_young", "al-D-roy": "al_rookie",
    "nl-D-roy": "nl_rookie", "ws-D-mvp": "world_series_mvp",
    "hart-D-w": "hart", "norris-D-w": "norris", "vezina-D-w": "vezina", "jackadams-D-w": "jack_adams", "calder-D-w": "calder",
    "D-mostgoals": "goals_leader", "D-mostpts": "points_leader",
    "heisman-D-w": "heisman", "uefa-bdor-D-w": "ballon_dor",
}
# A season's leaders.
LEADER_FUTURES = {
    "mostpassyds-D": "passing_yards_leader", "mostrecyds-D": "receiving_yards_leader", "mostrushyds-D": "rushing_yards_leader",
    "mostpasstds-D": "passing_touchdowns_leader", "mostrectds-D": "receiving_touchdowns_leader",
    "mostrushtds-D": "rushing_touchdowns_leader", "mostsacks-D": "sacks_leader", "mostintplayer-D": "interceptions_leader",
    "mostpassint-D": "interceptions_thrown_leader",
    "D-topscorer": "goals_leader", "D-mostast": "assists_leader",
}
# Leaders whose shape names the stat: a college conference's, 'cfb-sec-D-mostpassyds', and the baseball postseason's, 'mlb-pshrs-D-leader'.
COLLEGE_LEADER_EVENT = re.compile(r"^(sec|bigten|big12|acc)-D-most(passyds|passtds|recyds|rushyds|sacks)$")
COLLEGE_LEAGUES = {"sec": "sec", "bigten": "big_ten", "big12": "big_12", "acc": "acc"}
COLLEGE_STATS = {"passyds": "passing_yards_leader", "passtds": "passing_touchdowns_leader", "recyds": "receiving_yards_leader",
                 "rushyds": "rushing_yards_leader", "sacks": "sacks_leader"}
POSTSEASON_LEADER_EVENT = re.compile(r"^ps(hrs|rbi|ks|runs|sbs)-D-leader$")
POSTSEASON_STATS = {"hrs": "postseason_home_runs_leader", "rbi": "postseason_rbi_leader", "ks": "postseason_strikeouts_leader",
                    "runs": "postseason_runs_leader", "sbs": "postseason_stolen_bases_leader"}
# Champions and title holders.
TITLE_FUTURES = {
    "dc-D-w": "drivers_champion", "cupseries-D-w": "cup_series_champion", "oreillyseries-D-w": "oreilly_series_champion",
    "truckseries-D-w": "truck_series_champion", "worldchamp-D-w": "world_champion",
    "atp-D-no1": "atp_year_end_no1", "wta-D-no1": "wta_year_end_no1",
    **{f"{short}-D-champ": f"{division}_champion" for short, division in (
        ("flyw", "flyweight"), ("bantamw", "bantamweight"), ("featherw", "featherweight"), ("lightw", "lightweight"),
        ("welterw", "welterweight"), ("middlew", "middleweight"), ("lightheavyw", "light_heavyweight"), ("heavyw", "heavyweight"))},
}

# SEASON TOTALS, whose market title holds the line. One event a team, 'nfl-wins-D-ari', or one for all, the team ending the market slug.
TEAM_LINE_EVENTS = {re.compile(r"^wins-D-([a-z]+)$"): "season_wins", re.compile(r"^pts-D-([a-z]+)$"): "season_points"}
LINE_FUTURES = {"wins-ou-D": "season_wins", "D-wintotals": "season_wins", "D-teampts": "season_points", "D-points": "season_points"}
# One event per line, the team ending the market slug and its title the team: 'nhl-pts100-D' is 100 points or more.
SHAPE_LINE_EVENT = re.compile(r"^pts(\d+)-D$")
# A player's season total, one event per line, its title the player: 'nfl-D-1000recyds' is 1,000 yards or more.
PLAYER_LINE_EVENT = re.compile(r"^D-(\d+)(passyds|passtds|recyds|rectds|rushyds|rushtds)$")
PLAYER_STATS = {"passyds": "season_passing_yards", "passtds": "season_passing_touchdowns", "recyds": "season_receiving_yards",
                "rectds": "season_receiving_touchdowns", "rushyds": "season_rushing_yards", "rushtds": "season_rushing_touchdowns"}
LINE = re.compile(r"(\d+(?:\.\d+)?)\+")      # '2.5+ Wins' is over 2.5, 'Atlanta 43+ wins' at least 43.
WRONG_YEAR_DAYS = 180       # An event that ends this long after its slug's date has the wrong year in its slug.

# ELECTIONS. Control of a house, 'usho-midterms-D', and a race, 'usse-ga-D', 'usgub-mi-D', or 'ushr-az-01-D'.
CONTROL_EVENT = re.compile(r"^(usho|usse)-midterms-(\d{4})-\d{2}-\d{2}$")
CONTROLS = {"usho": "house_control", "usse": "senate_control"}
RACE_EVENT = re.compile(r"^(usse|usgub|ushr)-([a-z]{2})(?:-(\d{2}|al))?-(\d{4})-\d{2}-\d{2}$")
RACES = {"usse": "senate_race", "usgub": "governor_race", "ushr": "house_race"}
PARTIES = {"dem": "D", "rep": "R"}      # A market slug's end for a party's nominee, whoever it is.
PARTY_NOTE = re.compile(r"\s*\([A-Za-z]+\)$")   # The party after a candidate's name, 'Jon Ossoff (D)'.


def team(code, sport):
    """
    The sport's team a Polymarket US slug code names, or None.
    """
    return team_from_code(code, sport, "polymarket_us")


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
    What a title may start with to name a team: its codes on either venue and the first word of each of its names.
    """
    codes = [c for venue in VENUES for c in venue_codes(entry, venue)]
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
    The strict threshold a season total's market title states, or None: over 2.5 in '2.5+ Wins' is 2.5, at least 43 in
    'Atlanta 43+ wins' is 42.5, and over 3,750.5 in '3,750.5+ Passing Yards' is 3750.5.
    """
    m = LINE.search((title or "").replace(",", ""))
    return math.ceil(float(m.group(1))) - 0.5 if m else None


def shape_of(event, sport):
    """
    The event slug's prefix, its shape without the prefix with its first date as D, and that date, or None when the slug
    starts with none of the sport's prefixes or has no date.
    """
    prefix = next((p for p in EVENT_PREFIX.get(sport, ()) if event.startswith(p + "-")), None)
    date = DATE.search(event)
    if not prefix or not date:
        return None
    return prefix, DATE.sub("D", event[len(prefix) + 1:], count=1), date.group(0)


def looked_up(table, prefix, shape):
    """
    A shape's entry in a table, by the shape with its prefix first, then without.
    """
    return table.get(f"{prefix}-{shape}", table.get(shape))


def classify_future(row, sport, base):
    """
    The Bet a futures contract row describes, or None. Its season is the one the slug's date falls in, or the event's end
    when that is over WRONG_YEAR_DAYS later.
    """
    found = shape_of(row["event_id"], sport)
    if not found:
        return None
    prefix, shape, date = found
    end = (row["close_time"] or date)[:10]
    season = season_from_date(end if days_between(date, end) > WRONG_YEAR_DAYS else date, sport)
    future = dict(season=season, game_date=None, team_a=None, team_b=None, polarity="yes", **base)
    suffix = team(row["contract_id"].rsplit("-", 1)[-1], sport)
    kind = looked_up(AWARD_FUTURES, prefix, shape) or looked_up(LEADER_FUTURES, prefix, shape) or looked_up(TITLE_FUTURES, prefix, shape)
    m = COLLEGE_LEADER_EVENT.match(shape)
    kind = kind or (f"{COLLEGE_LEAGUES[m.group(1)]}_{COLLEGE_STATS[m.group(2)]}" if m else None)
    m = POSTSEASON_LEADER_EVENT.match(shape)
    kind = kind or (POSTSEASON_STATS[m.group(1)] if m else None)
    if kind:
        subject = person(row["title"])
        return Bet(kind=kind, subject=subject, line=None, **future) if subject else None
    m = PLAYER_LINE_EVENT.match(shape)
    if m:
        subject = person(row["title"])
        return Bet(kind=PLAYER_STATS[m.group(2)], subject=subject, line=int(m.group(1)) - 0.5, **future) if subject else None
    m = SHAPE_LINE_EVENT.match(shape)
    if m:
        subject = future_team(row, sport)
        return Bet(kind="season_points", subject=subject, line=int(m.group(1)) - 0.5, **future) if subject else None
    team_line = next(((pattern.match(shape), kind) for pattern, kind in TEAM_LINE_EVENTS.items() if pattern.match(shape)), None)
    if team_line or shape in LINE_FUTURES:
        subject = team(team_line[0].group(1), sport) if team_line else suffix
        line = strict_line(row["title"])
        kind = team_line[1] if team_line else LINE_FUTURES[shape]
        return Bet(kind=kind, subject=subject, line=line, **future) if subject and line is not None else None
    m = SERIES_EVENT.match(shape)
    if m:
        teams = (team(m.group(2), sport), team(m.group(3), sport))
        if suffix not in teams or None in teams:
            return None
        team_a, team_b = sorted(teams)      # The venues list a series' teams in different orders.
        return Bet(kind="wild_card_series", subject=suffix, line=None, **dict(future, team_a=team_a, team_b=team_b))
    if shape == CONFERENCE_EVENT and sport == "ncaaf":
        subject = CONFERENCE_CODES.get(row["contract_id"].rsplit("-", 1)[-1])
        return Bet(kind="champion_conference", subject=subject, line=None, **future) if subject else None
    kind = looked_up(TEAM_FUTURES, prefix, shape)
    subject = future_team(row, sport) if kind else None
    return Bet(kind=kind, subject=subject, line=None, **future) if subject else None


def race_side(state, row):
    """
    Who a race's market is on, a party, 'D' or 'R', or a candidate's name key, or None: in a state whose general election
    can pit two of one party, see teams.TOP_TWO_STATES, only a candidate.
    """
    tail = row["contract_id"].rsplit("-", 1)[-1]
    if tail in PARTIES:
        return PARTIES[tail] if state not in TOP_TWO_STATES else None
    return person(PARTY_NOTE.sub("", row["title"] or ""))


def classify_election(row, base):
    """
    The Bet an election contract row describes, or None. Its season is the election's year.
    """
    election = dict(game_date=None, team_a=None, team_b=None, line=None, polarity="yes", **base)
    m = CONTROL_EVENT.match(row["event_id"])
    if m:
        party = PARTIES.get(row["contract_id"].rsplit("-", 1)[-1])
        return Bet(kind=CONTROLS[m.group(1)], season=int(m.group(2)), subject=party, **election) if party else None
    m = RACE_EVENT.match(row["event_id"])
    if not m:
        return None
    office, state, district, year = m.group(1), m.group(2).upper(), m.group(3), int(m.group(4))
    if state not in STATES or (office == "ushr") != (district is not None):
        return None
    side = race_side(state, row)
    return Bet(kind=RACES[office], season=year, subject=f"{race(state, district)} {side}", **election) if side else None


def game_event(row, sport):
    """
    A game, match, or race event's (first side's code, second side's code, date), or None when the event slug is not
    one of the sport's.
    """
    m = GAME_EVENT.match(row["event_id"])
    if not m or m.group(1) not in EVENT_PREFIX.get(sport, ()):
        return None
    return m.group(2), m.group(3), m.group(4)


def signed_spread(first, second, line, common, kind):
    """
    A spread's Bet from its line, signed for the first side: a negative line is the first side winning by more than it,
    a positive one the second side not winning by more than it.
    """
    if line < 0:
        return Bet(kind=kind, subject=first, line=-line, polarity="yes", **common)
    return Bet(kind=kind, subject=second, line=line, polarity="no", **common)


def classify_game(row, sport, base):
    """
    The Bet a team game's contract describes, or None. Its date is the kickoff's in Eastern time.
    """
    found = game_event(row, sport)
    if not found:
        return None
    away, home = team(found[0], sport), team(found[1], sport)
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
        return Bet(kind=kind, subject=player_key(name.group(1)), line=row["line"] - 0.5, polarity="yes", **common) if row["line"] is not None else None
    if kind == "game_winner":
        return Bet(kind=kind, subject=away, line=None, polarity="yes", **common)
    if row["line"] is None:
        return None
    if kind == "team_total":
        code = TEAM_TOTAL.search(row["contract_id"])
        subject = team(code.group(1), sport) if code else None
        return Bet(kind=kind, subject=subject, line=row["line"], polarity="yes", **common) if subject in (away, home) else None
    if kind == "spread":
        return signed_spread(away, home, row["line"], common, kind)
    return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common)


def classify_soccer(row, sport, base):
    """
    The Bet a soccer match's contract describes, or None. The clubs are kept in order of their codes, as on Kalshi, and
    a result's market slug ends in the club or 'draw', 'atc-epl-ars-lee-2026-10-10-fh-draw'.
    """
    found = game_event(row, sport)
    kind = SOCCER_KINDS.get(row["market_type"])
    if not found or not kind:
        return None
    first, second = team(found[0], sport), team(found[1], sport)
    if not (first and second):
        return None
    common = dict(season=season_from_date(found[2], sport), game_date=found[2], team_a=min(first, second), team_b=max(first, second),
                  **base)
    if kind.endswith("result"):
        tail = row["contract_id"].rsplit("-", 1)[-1]
        subject = "tie" if tail == "draw" else team(tail, sport)
        return Bet(kind=kind, subject=subject, line=None, polarity="yes", **common) if subject in (first, second, "tie") else None
    if kind.endswith("btts"):
        return Bet(kind=kind, subject=None, line=None, polarity="yes", **common)
    if kind.endswith("exact_score"):
        m = SCORE_SLUG.search(row["contract_id"])
        if not m:
            return None
        goals = {first: m.group(1), second: m.group(2)}
        return Bet(kind=kind, subject=" ".join(f"{club}{goals[club]}" for club in sorted(goals)), line=None, polarity="yes", **common)
    if row["line"] is None or (kind == "total_corners" and not CORNERS_SLUG.search(row["contract_id"])):
        return None
    if kind.endswith("spread"):
        return signed_spread(first, second, row["line"], common, kind)
    return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common)


def classify_match(row, sport, base):
    """
    The Bet a match between two people describes, or None. The event title names both, first and second, 'Valentin
    Vacherot vs. Arthur Fils', and the two are kept in order of their keys, as on Kalshi, a winner's market being the
    first of them winning or its complement.
    """
    found = game_event(row, sport)
    kind = MATCH_KINDS.get(row["market_type"])
    sides = match_sides(row["event_title"]) if found and kind else None
    if not sides:
        return None
    named_first, named_second = sides
    first, second = sorted(sides)
    common = dict(season=int(found[2][:4]), game_date=found[2], team_a=first, team_b=second, **base)
    if kind == "match_winner":
        picked = side_key(row["outcome"])
        return Bet(kind=kind, subject=first, line=None, polarity="yes" if picked == first else "no", **common) if picked in sides else None
    if kind == "set_winner":
        m = SET_TITLE.match(row["title"] or "")
        picked = side_key(m.group(1)) if m else None
        if picked not in sides:
            return None
        return Bet(kind=f"set_{m.group(2)}_winner", subject=first, line=None, polarity="yes" if picked == first else "no", **common)
    if kind == "exact_score":
        m = SETS_SLUG.search(row["contract_id"])
        if not m or m.group(1) == m.group(2):
            return None
        won, lost = max(m.group(1, 2)), min(m.group(1, 2))
        winner = named_first if m.group(1) > m.group(2) else named_second
        return Bet(kind=kind, subject=f"{winner} {won}-{lost}", line=None, polarity="yes", **common)
    if kind == "round_of_victory":
        m = ROUND_SLUG.search(row["contract_id"])
        if not m:
            return None
        return Bet(kind=kind, subject=named_first if m.group(1) == "1" else named_second, line=float(m.group(2)), polarity="yes", **common)
    if kind == "go_the_distance":
        m = DISTANCE_SLUG.search(row["contract_id"])
        return Bet(kind=kind, subject=None, line=None, polarity="yes" if m.group(1) == "yes" else "no", **common) if m else None
    if row["line"] is None:
        return None
    if kind in ("games_spread", "sets_spread"):
        return signed_spread(named_first, named_second, row["line"], common, kind)
    return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common)


def classify_race(row, sport, base):
    """
    The Bet on a race's winner, 'f1-gabgpim-2026-10-04-w', whose market title is the driver, or its winning constructor,
    'f1-gabgpim-2026-10-04-cons', whose market slug ends in the constructor. A race is its date.
    """
    m = RACING_EVENT.match(row["event_id"])
    if not m:
        return None
    if m.group(4) == "cons":
        kind, subject = "race_constructor", team(row["contract_id"].rsplit("-", 1)[-1], sport)
    else:
        kind, subject = "race_winner", side_key(row["title"])
    race_date = m.group(3)
    return Bet(kind=kind, season=int(race_date[:4]), game_date=race_date, team_a=None, team_b=None, subject=subject, line=None,
               polarity="yes", **base) if subject else None


def rule_end(rules):
    """
    The last day a price rule counts, 'before 12:00 AM ET on January 1, 2027' being December 31, 2026, or None.
    """
    m = RULE_END.search(rules or "")
    day = written_date(m.group(4)) if m else None
    return last_day(day, *m.group(1, 2, 3)) if day else None


def classify_crypto(row, base):
    """
    The Bet a Bitcoin contract describes, or None: a window's direction, a price going above or below its strike by a
    deadline, or the band it ends the year in.
    """
    m = UPDOWN_EVENT.match(row["event_id"])
    if m:
        return Bet(kind=f"updown_{m.group(1)}", season=int(m.group(2)[:4]), game_date=f"{m.group(2)} {m.group(3)}:{m.group(4)}",
                   team_a=None, team_b=None, subject="BTC", line=None, polarity="yes", **base)
    last = rule_end(row["rules"])
    if not last or not row["event_id"].startswith("btc-"):
        return None
    if row["event_id"].startswith(RANGE_EVENT):
        band = (row["title"] or "").replace("$", "")
        numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", band)]
        if "or above" in band and len(numbers) == 1:
            subject = f"from {numbers[0]:.0f}"
        elif ("or below" in band or band.lower().startswith("below")) and len(numbers) == 1:
            subject = f"below {numbers[0] + (0.01 if 'or below' in band else 0):.0f}"
        elif " to " in band and len(numbers) == 2:
            subject = f"{numbers[0]:.0f} to {numbers[1] + 0.01:.0f}"
        else:
            return None
        return Bet(kind="year_end_range", season=int(last[:4]), game_date=None, team_a=None, team_b=None, subject=subject, line=None,
                   polarity="yes", **base)
    m = PRICE_RULE.search(row["rules"] or "")
    if not m:
        return None
    price = m.group(2) or next(iter(PRICE_TITLE.findall(row["title"] or "")), None)
    if not price:
        return None
    return Bet(kind="hit_before" if m.group(1) == "above" else "dip_before", season=int(last[:4]), game_date=None, team_a=None,
               team_b=None, subject=last, line=float(price.replace(",", "")), polarity="yes", **base)


def doubleheaders(rows):
    """
    The contracts on games the same teams play twice on one date, which a
    bet cannot tell apart yet, since a game is its date and teams. Their
    event slugs end in -dh1 and -dh2, though a first game may come without,
    so a date and teams with two events counts too.
    """
    events, games = defaultdict(set), {}
    for row in rows:
        m = GAME_EVENT.match(row["event_id"]) if row["sport"] in TEAM_SPORTS else None
        if m and m.group(1) in EVENT_PREFIX[row["sport"]]:
            game = (row["sport"], *m.group(2, 3, 4))
            events[game].add(row["event_id"])
            games[row["contract_id"]] = (game, bool(m.group(5)))
    return {contract_id for contract_id, (game, suffixed) in games.items() if suffixed or len(events[game]) > 1}


def classify(row, outcomes=None):
    """
    The Bet a Polymarket US contract row describes, or None when it is not one we trade. outcomes is unused: the
    venue's event titles name a match's people in full.
    """
    base = dict(venue=row["venue"], contract_id=row["contract_id"])
    sport = row["sport"]
    if sport == "politics":
        return classify_election(row, base)
    if sport == "crypto":
        return classify_crypto(row, base)
    if row["market_type"] == "futures":
        # Before a race, whose winner is typed a future too: 'f1-dc-2026-12-06-w' is the drivers' title.
        return classify_future(row, sport, base) or (classify_race(row, sport, base) if sport in RACING else None)
    if sport in TEAM_SPORTS:
        return classify_game(row, sport, base)
    if sport in SOCCER:
        return classify_soccer(row, sport, base)
    if sport in MATCH_SPORTS:
        return classify_match(row, sport, base)
    return None
