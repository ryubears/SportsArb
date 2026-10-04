"""
Turn Kalshi contracts into Bets.

Games, matches, races, and Bitcoin's 15 minute windows are bets on one
event, which paper trades: the series says the kind, the event ticker the
date and the two sides, and the market ticker or subtitle whom the market
is on, see the tables below for each sport's layout. A game of teams keeps
them away then home, as the venues list them, and a soccer match or a
match between two people keeps its sides in order of their keys, so a
market names whom it is on by its subject.

Futures are bets on a season, a title, an award, a season's leader, a price
by a deadline, or an election, which live trades. Each kind has a series of
its own, or an event of its own within a series, 'KXEPLTOP-27TOP4' the top
four of the Premier League, which the event's key names once its year is
taken out. A team future's market ticker ends in the team, 'KXSB-27-KC'. An
award's, a leader's, or a title holder's names the person in its subtitle,
'Aaron Judge'. A team's season total has one event per team,
'KXNFLWINS-27ARI', and one market per line, and a player's has one event
per line and one market per player. The venues number seasons differently,
so a future's season is the one its settlement falls in, which both agree
on.

Elections have a series per office and state, 'SENATEGA', one event per
election year, '-26', and a market per party, D or R, or per candidate.
A race's season is its election year. In a state whose general election
can put two of one party on the ballot, California's and Washington's top
two and Alaska's top four, the venues read a party's win differently, so
there only the candidates are paired. This is the only file that knows
Kalshi's ticker layout.
"""

import re
from catalog.classify.teams import STATES, TOP_TWO_STATES, match_sides, person, player_key, race, side_key, team_from_code
from collections import defaultdict
from common.timeutil import last_day, season_from_date, shift, written_date
from datetime import datetime
from db.models import Bet

# GAMES of teams. The series says the kind, the event ticker the date and the teams, away then home, 'KXNFLGAME-26SEP20CARATL',
# with baseball's Eastern start time, 'KXMLBGAME-26SEP291400PHIATL', and the market ticker the team or the line.
GAME_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z]+)$")
GAME_SERIES = {
    "KXNFLGAME": "game_winner", "KXNFLSPREAD": "spread", "KXNFLTOTAL": "total",
    "KXNCAAFGAME": "game_winner", "KXNCAAFSPREAD": "spread", "KXNCAAFTOTAL": "total",
    "KXMLBGAME": "game_winner", "KXMLBSPREAD": "spread", "KXMLBTOTAL": "total", "KXMLBTEAMTOTAL": "team_total",
    "KXNHLGAME": "game_winner", "KXNHLSPREAD": "spread", "KXNHLTOTAL": "total", "KXNHLTEAMTOTAL": "team_total",
    "KXNBAGAME": "game_winner", "KXNBASPREAD": "spread", "KXNBATOTAL": "total", "KXNBATEAMTOTAL": "team_total",
    "KXWNBAGAME": "game_winner", "KXWNBASPREAD": "spread", "KXWNBATOTAL": "total", "KXWNBATEAMTOTAL": "team_total",
    "KXNCAAMBGAME": "game_winner", "KXNCAAMBSPREAD": "spread", "KXNCAAMBTOTAL": "total",
}
# Player props on one game, the player named in the title before the colon, 'Bijan Robinson: 100+ receiving yards'. Every one
# but the first touchdown carries a line, stored as the strict threshold.
PLAYER_SERIES = {
    "KXNFLRECYDS": "player_receiving_yards", "KXNFLRSHYDS": "player_rushing_yards", "KXNFLPASSYDS": "player_passing_yards",
    "KXNFLREC": "player_receptions", "KXNFLPASSTDS": "player_passing_touchdowns", "KXNFLTD": "player_touchdowns",
    "KXNFLFIRSTTD": "player_first_touchdown", "KXNFLPASSCOMP": "player_passing_completions", "KXNFLPASSATT": "player_passing_attempts",
    "KXNFLPASSINT": "player_interceptions_thrown", "KXNFLRSHATT": "player_rushing_attempts", "KXNFLRRYDS": "player_scrimmage_yards",
    "KXNFLLONGREC": "player_longest_reception",
    "KXMLBHIT": "player_hits", "KXMLBHR": "player_home_runs", "KXMLBKS": "player_strikeouts", "KXMLBTB": "player_total_bases",
    "KXMLBHRR": "player_hits_runs_rbis", "KXMLBRBI": "player_rbis", "KXMLBSB": "player_stolen_bases", "KXMLBOUTS": "player_outs",
    "KXMLBHA": "player_hits_allowed", "KXMLBERA": "player_earned_runs_allowed", "KXMLBWA": "player_walks_allowed",
    "KXNHLGOAL": "player_goals", "KXNHLPTS": "player_points",
    "KXNBAPTS": "player_points", "KXNBAREB": "player_rebounds", "KXNBAAST": "player_assists", "KXNBA3PT": "player_threes",
    "KXNBABLK": "player_blocks",
    "KXWNBAPTS": "player_points", "KXWNBAREB": "player_rebounds", "KXWNBAAST": "player_assists", "KXWNBA3PT": "player_threes",
}
PLAYER_TITLE = re.compile(r"^(.+?): ")

# SOCCER MATCHES, each league's series named alike, 'KXEPLGAME' and 'KXEPL1HTOTAL', the event ticker the date and the clubs,
# home then away, 'KXEPLGAME-26OCT10ARSLEE'. A result's market is a club or 'TIE', a spread's a club and its rounded margin,
# 'ARS2', a score's each club's goals, 'FUL0MUN1', and a total's or the corners' its line. Every one counts 90 minutes and
# stoppage time, a half's 45 and its stoppage, but the corners count any extra time too.
SOCCER_LEAGUES = {"epl": "EPL", "laliga": "LALIGA", "seriea": "SERIEA", "bundesliga": "BUNDESLIGA", "ligue1": "LIGUE1",
                  "ligamx": "LIGAMX", "mls": "MLS", "ucl": "UCL", "uel": "UEL"}
SOCCER_KINDS = {
    "GAME": "result", "1H": "first_half_result", "2H": "second_half_result",
    "SPREAD": "spread", "1HSPREAD": "first_half_spread", "2HSPREAD": "second_half_spread",
    "TOTAL": "total", "1HTOTAL": "first_half_total", "2HTOTAL": "second_half_total",
    "BTTS": "btts", "1HBTTS": "first_half_btts", "2HBTTS": "second_half_btts",
    "SCORE": "exact_score", "1HSCORE": "first_half_exact_score", "CORNERS": "total_corners",
}
SOCCER_SERIES = {f"KX{league}{suffix}": kind for league in SOCCER_LEAGUES.values() for suffix, kind in SOCCER_KINDS.items()}
SCORE_TAIL = re.compile(r"^([A-Z]+?)(\d+)([A-Z]+?)(\d+)$")

# MATCHES between two people, in tennis, darts, and the UFC. The event ticker holds the date and both short names,
# 'KXATPMATCH-26OCT03VACFIL', darts' with the Eastern start time, and a set's market its number, 'KXATPSETWINNER-26OCT03VACFIL-1'.
# The market's subtitle names whom it is on: 'Arthur Fils', 'Arthur Fils -6.5 games', 'Arthur Fils wins 2-0', or 'Natalia
# Silva to win in Round 1'. The event title names both by their full names, 'Valentin Vacherot vs Arthur Fils: Total Games',
# bar a match winner's, 'Vacherot vs Fils', whose two markets do instead.
MATCH_SERIES = {
    "KXATPMATCH": "match_winner", "KXWTAMATCH": "match_winner", "KXATPCHALLENGERMATCH": "match_winner",
    "KXWTACHALLENGERMATCH": "match_winner",
    "KXATPGSPREAD": "games_spread", "KXATPGAMESPREAD": "games_spread", "KXWTAGSPREAD": "games_spread",
    "KXATPGTOTAL": "total_games", "KXATPGAMETOTAL": "total_games", "KXWTAGTOTAL": "total_games",
    "KXATPSSPREAD": "sets_spread", "KXATPTOTALSETS": "total_sets",
    "KXATPSETWINNER": "set_winner", "KXWTASETWINNER": "set_winner",
    "KXATPEXACTMATCH": "exact_score", "KXWTAEXACTMATCH": "exact_score",
    "KXUFCFIGHT": "match_winner", "KXUFCDISTANCE": "go_the_distance", "KXUFCVICROUND": "round_of_victory",
    "KXDARTSMATCH": "match_winner",
}
MATCH_EVENT = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z]+)(?:-(\d))?$")
SPREAD_SIDE = re.compile(r"^(.+?) -\d+(?:\.\d+)? (?:games|sets)$")      # 'Arthur Fils -6.5 games'.
SCORE_SIDE = re.compile(r"^(.+) wins (\d)-(\d)$")                       # 'Valentin Vacherot wins 2-0'.
ROUND_SIDE = re.compile(r"^(.+) to win in Round (\d)$")                 # 'Natalia Silva to win in Round 1'.

# RACES, one event a race, 'KXF1RACE-BAH26', a market per driver, named in its subtitle, or per constructor, by its code.
# The date is the one the rules give the race, 'originally scheduled for October 4, 2026'.
RACING_SERIES = {"KXF1RACE": "race_winner", "KXF1TOPCONSTRUCTOR": "race_constructor", "KXNASCARRACE": "race_winner"}
SCHEDULED = re.compile(r"scheduled for ([A-Z][a-z]+\.? \d{1,2}, \d{4})")

# CRYPTO, Bitcoin only, the one coin Polymarket US lists. A 15 minute window's event ticker gives its Eastern end,
# 'KXBTC15M-26OCT040145', and the window is the 15 minutes before its close. A price future pays on the CF Bitcoin Real-Time
# Index going above, or below, its strike before a deadline its rules give, 'before Sep 1, 2026 at 12:00 AM ET' being the
# end of August 31, or on where it ends the year, in a band of $5,000.
UPDOWN_SERIES = {"KXBTC15M": ("updown_15m", "BTC", 15)}
HIT_SERIES = {"KXBTCMAXY": "hit_before", "KXBTCMAX150": "hit_before", "KXBTCMAX100": "hit_before", "KXBTC2026200": "hit_before",
              "KXBTC2026250": "hit_before", "KXBTCMINY": "dip_before"}
RANGE_SERIES = {"KXBTCY": "year_end_range"}
DEADLINE = re.compile(r"(?:by|before) ([A-Z][a-z]+\.? \d{1,2},? \d{4})(?:,? at (\d{1,2}):(\d{2}) ?([AaPp][Mm]))?")
CRYPTO_SERIES = {*UPDOWN_SERIES, *HIT_SERIES, *RANGE_SERIES}

# TEAM FUTURES. The market ticker ends in the team, 'KXSB-27-KC'.
CONFERENCES = ("AAC", "ACC", "B10", "B12", "CUSA", "MAC", "MWC", "PAC12", "SBELT")     # College conferences, bar the SEC, spelled alike.
NFL_DIVISIONS = tuple(f"{c}{d}" for c in ("AFC", "NFC") for d in ("EAST", "NORTH", "SOUTH", "WEST"))     # 'KXNFLAFCEAST'.
NHL_DIVISIONS = ("ATLANTIC", "CENTRAL", "METROPOLITAN", "PACIFIC")     # 'KXNHLATLANTIC'.
TEAM_FUTURES = {
    "KXSB": "champion", "KXMLB": "champion", "KXNBA": "champion", "KXNHL": "champion", "KXNCAAF": "champion",
    "KXNFLAFCCHAMP": "conf_champion", "KXNFLNFCCHAMP": "conf_champion", "KXMLBAL": "conf_champion", "KXMLBNL": "conf_champion",
    "KXNBAEAST": "conf_champion", "KXNBAWEST": "conf_champion", "KXNHLEAST": "conf_champion", "KXNHLWEST": "conf_champion",
    **{f"KXNCAAF{c}": "conf_champion" for c in (*CONFERENCES, "SEC")},
    **{f"KXNFL{d}": "division_champion" for d in NFL_DIVISIONS}, **{f"KXNHL{d}": "division_champion" for d in NHL_DIVISIONS},
    "KXNFL1SEED": "conf_top_seed", "KXNBAEAST1SEED": "conf_top_seed", "KXNBAWEST1SEED": "conf_top_seed",
    "KXNFLPLAYOFF": "make_playoffs", "KXNHLPLAYOFF": "make_playoffs", "KXNCAAFPLAYOFF": "make_playoffs",
    "KXMLBALCSQUAL": "reach_conf_final", "KXMLBNLCSQUAL": "reach_conf_final",     # The league championship series.
    "KXNCAAFFINALIST": "reach_final",
    **{f"KXNCAAF{c}QUAL": "reach_conf_title_game" for c in CONFERENCES}, "KXNCAAFSECQ": "reach_conf_title_game",
    "KXNHLPRES": "presidents_trophy",
    "KXRECORDNFLBEST": "best_record", "KXNFLLASTTOLOSE": "last_undefeated", "KXNFLLASTTOWIN": "last_winless",
    "KXNCAAFUNDEFEATED": "undefeated",
    "KXWNBA": "champion", "KXMARMAD": "champion",
    "KXPREMIERLEAGUE": "champion", "KXEPLRELEGATION": "relegated", "KXEPLLAST": "last_place",
    "KXLALIGA": "champion", "KXLALIGARELEGATION": "relegated", "KXLALIGALAST": "last_place",
    "KXSERIEA": "champion", "KXSERIEARELEGATION": "relegated", "KXSERIEALAST": "last_place",
    "KXBUNDESLIGA": "champion", "KXBUNDESLIGARELEGATION": "relegated", "KXBUNDESLIGALAST": "last_place",
    "KXLIGUE1": "champion", "KXLIGUE1RELEGATION": "relegated", "KXLIGUE1LAST": "last_place",
    "KXMLSCUP": "champion", "KXMLSEAST": "conf_champion", "KXMLSWEST": "conf_champion",
    "KXUCL": "champion", "KXUCLTOP": "league_phase_top", "KXUCLTOP8": "league_phase_top_8", "KXUCLBOTTOM": "league_phase_last",
    "KXUEL": "champion",
    "KXF1CONSTRUCTORS": "constructors_champion",
}
# Team futures whose kind is the event's, by its key: 'KXEPLTOP-27TOP4' is the top four.
EVENT_TEAM_FUTURES = {
    "KXNFLROUNDQUAL": {"CONF": "reach_conf_final", "DIV": "reach_divisional_round"},     # Playoff rounds, 'KXNFLROUNDQUAL-27CONF'.
    "KXNBARECORD": {"BEST": "best_record", "WORST": "worst_record"},
    "KXEPLTOP": {"TOP2": "top_2", "TOP4": "top_4", "TOP6": "top_6", "TOPHALF": "top_half"},
    "KXLALIGATOP": {"TOP4": "top_4", "TOP6": "top_6"},
    "KXSERIEATOP": {"TOP4": "top_4"}, "KXBUNDESLIGATOP": {"TOP4": "top_4"}, "KXLIGUE1TOP": {"TOP4": "top_4"},
    "KXLIGAMX": {"APER": "apertura_champion", "CLA": "clausura_champion"},
    "KXUCLROUND": {"RO16": "reach_round_of_16", "QUAR": "reach_quarterfinal", "SEMI": "reach_semifinal", "FINAL": "reach_final"},
}
# The conference that wins college football's title, 'KXNCAAFCONF-26-B10'.
CONFERENCE_SERIES = "KXNCAAFCONF"
CONFERENCE_CODES = {"B10": "big_ten", "SEC": "sec", "B12": "big_12", "ACC": "acc"}
SERIES_WINNERS = "KXMLBSERIES"      # One event per playoff series, the round last: 'KXMLBSERIES-26PHIATLWC'.
SERIES_ROUNDS = {"WC": "wild_card_series"}
SERIES_EVENT = re.compile(r"^\d{2}([A-Z]+?)(WC|DS|CS|WS)$")

# PERSON FUTURES. The market's subtitle is the person, 'Aaron Judge'.
AWARD_FUTURES = {
    "KXNFLMVP": "mvp", "KXNFLOPOTY": "offensive_player", "KXNFLDPOTY": "defensive_player", "KXNFLOROTY": "offensive_rookie",
    "KXNFLDROTY": "defensive_rookie", "KXNFLCPOTY": "comeback_player", "KXNFLCOTY": "coach",
    "KXMLBALMVP": "al_mvp", "KXMLBNLMVP": "nl_mvp", "KXMLBALCY": "al_cy_young", "KXMLBNLCY": "nl_cy_young",
    "KXMLBALROTY": "al_rookie", "KXMLBNLROTY": "nl_rookie", "KXMLBWSMVP": "world_series_mvp",
    "KXNBAMVP": "mvp", "KXWNBAMVP": "mvp",
    "KXNHLHART": "hart", "KXNHLNORRIS": "norris", "KXNHLVEZINA": "vezina", "KXNHLADAMS": "jack_adams", "KXNHLCALDER": "calder",
    "KXNHLRICHARD": "goals_leader", "KXNHLROSS": "points_leader",
    "KXHEISMAN": "heisman", "KXBALLONDOR": "ballon_dor",
}
# A season's leaders, whose ties both venues' rules treat as they do an award's.
LEADER_FUTURES = {
    "KXLEADERNFLPYDS": "passing_yards_leader", "KXLEADERNFLRYDS": "receiving_yards_leader", "KXLEADERNFLRUSHYDS": "rushing_yards_leader",
    "KXLEADERNFLPTDS": "passing_touchdowns_leader", "KXLEADERNFLRTDS": "receiving_touchdowns_leader",
    "KXLEADERNFLRUSHTDS": "rushing_touchdowns_leader", "KXLEADERNFLSACKS": "sacks_leader", "KXLEADERNFLINT": "interceptions_leader",
    "KXLEADERNFLPINT": "interceptions_thrown_leader",
}
# Champions and title holders, who win it outright.
TITLE_FUTURES = {
    "KXF1": "drivers_champion", "KXNASCARCUPSERIES": "cup_series_champion", "KXNASCARAUTOPARTSSERIES": "oreilly_series_champion",
    "KXNASCARTRUCKSERIES": "truck_series_champion", "KXPDCDARTS": "world_champion",
    "KXATP1RANK": "atp_year_end_no1", "KXWTA1RANK": "wta_year_end_no1",
    **{f"KXUFC{division.upper()}TITLE": f"{division}_champion" for division in (
        "flyweight", "bantamweight", "featherweight", "lightweight", "welterweight", "middleweight", "heavyweight")},
    "KXUFCLHEAVYWEIGHTTITLE": "light_heavyweight_champion",
}
COLLEGE_STATS = {"PASSYDS": "passing_yards_leader", "PASSTD": "passing_touchdowns_leader", "RECYDS": "receiving_yards_leader",
                 "RSHYDS": "rushing_yards_leader", "SACK": "sacks_leader"}
# Leaders whose kind is the event's, by its key: 'KXEPLLEADER-27GOAL' leads the Premier League in goals.
EVENT_LEADER_FUTURES = {
    **{series: {"GOAL": "goals_leader", "AST": "assists_leader"} for series in (
        "KXEPLLEADER", "KXLALIGALEADER", "KXSERIEALEADER", "KXBUNDESLIGALEADER", "KXLIGUE1LEADER", "KXUCLLEADER")},
    "KXMLBLEADERPLAYOFF": {"HR": "postseason_home_runs_leader", "RBI": "postseason_rbi_leader", "KS": "postseason_strikeouts_leader",
                           "RUNS": "postseason_runs_leader", "SB": "postseason_stolen_bases_leader"},
    # A college conference's, 'KXNCAAFSECLEADER-26PASSYDS'. The country's, KXNCAAFLEADER, counts all of Division I, where
    # Polymarket US's counts the FBS only, so it is not read.
    **{f"KXNCAAF{series}LEADER": {stat: f"{conference}_{kind}" for stat, kind in COLLEGE_STATS.items()}
       for series, conference in (("SEC", "sec"), ("BIGTEN", "big_ten"), ("BIG12", "big_12"), ("ACC", "acc"))},
}

# SEASON TOTALS. A team's, one event per team, 'KXNFLWINS-27ARI', and one market per line, stored as the strict threshold.
LINE_FUTURES = {"KXNFLWINS": "season_wins", "KXNCAAFWINS": "season_wins", "KXNBAWINS": "season_wins", "KXNHLSEASONPTS": "season_points"}
LINE_EVENT = re.compile(r"^\d{2}([A-Z]+)$")
# A team's with the team in the market ticker before its line, 'KXEPLTEAMPOINTS-27-ARS70'.
TEAM_LINE_FUTURES = {"KXEPLTEAMPOINTS": "season_points"}
# A player's, one event per line, 'KXNFLSEASONPASSYDS-27C3000', and one market per player, named in its subtitle.
PLAYER_LINE_FUTURES = {
    "KXNFLSEASONPASSYDS": "season_passing_yards", "KXNFLSEASONPASSTDS": "season_passing_touchdowns",
    "KXNFLSEASONRECYDS": "season_receiving_yards", "KXNFLSEASONRECTD": "season_receiving_touchdowns",
    "KXNFLSEASONRSHYDS": "season_rushing_yards", "KXNFLSEASONRSHTD": "season_rushing_touchdowns", "KXNFLSEASONREC": "season_receptions",
}

# ELECTIONS. The country's control of each house, 'CONTROLH-2026', with a market per party.
CONTROL_SERIES = {"CONTROLH": "house_control", "CONTROLS": "senate_control"}
# A race's series, by office and state, with the House district in it where it has one: 'SENATEGA', 'HOUSEAZ1', 'KXHOUSEIN7'.
RACE_SERIES = {re.compile(r"^SENATE([A-Z]{2})$"): "senate_race", re.compile(r"^GOVPARTY([A-Z]{2})$"): "governor_race",
               re.compile(r"^KXGOV([A-Z]{2})$"): "governor_race", re.compile(r"^(?:KX)?HOUSE([A-Z]{2})(\d+|AL)$"): "house_race"}
HOUSE_RACE_SERIES = "KXHOUSERACE"       # Every district in one series, an event each: 'KXHOUSERACE-CA01-26'.
HOUSE_RACE_EVENT = re.compile(r"^([A-Z]{2})(\d+|AL)-(\d{2})$")
PARTIES = {"D": "D", "R": "R"}          # A race's market tail for a party's candidate, whoever it is, by the party.

FUTURE_SERIES = {*TEAM_FUTURES, *EVENT_TEAM_FUTURES, CONFERENCE_SERIES, SERIES_WINNERS, *AWARD_FUTURES, *LEADER_FUTURES, *TITLE_FUTURES,
                 *EVENT_LEADER_FUTURES, *LINE_FUTURES, *TEAM_LINE_FUTURES, *PLAYER_LINE_FUTURES, *CONTROL_SERIES, HOUSE_RACE_SERIES}
SERIES_PATTERNS = tuple(RACE_SERIES)    # Series read by their shape, there being one per state or district.


def team(code, sport):
    """
    The sport's team a Kalshi ticker code names, or None.
    """
    return team_from_code(code, sport, "kalshi")


def split_codes(pair, sport):
    """
    Split two glued ticker codes such as 'PHIATL' into two of the sport's
    teams. Codes differ in length, so every split is tried, and one that is
    not the only split into two teams is refused, not guessed.
    """
    splits = [(a, b) for a, b in ((team(pair[:i], sport), team(pair[i:], sport)) for i in range(1, len(pair))) if a and b]
    return splits[0] if len(splits) == 1 else (None, None)


def event_key(event_tail):
    """
    An event's key, its tail with the season's two digits taken out: '27TOP4' is 'TOP4', and 'APER26GOAL' 'APERGOAL'.
    """
    return re.sub(r"\d{2}", "", event_tail, count=1)


def lookup(series):
    """
    The table a series is read by and its entry there, or (None, None).
    """
    for table in (TEAM_FUTURES, EVENT_TEAM_FUTURES, AWARD_FUTURES, LEADER_FUTURES, TITLE_FUTURES, EVENT_LEADER_FUTURES):
        if series in table:
            return table, table[series]
    return None, None


def classify_future(row, series, event_tail, market_tail, sport, base):
    """
    The Bet a futures contract row describes, or None. Its season is the one its settlement falls in.
    """
    future = dict(season=season_from_date(row["close_time"][:10], sport), game_date=None, polarity="yes", **base)
    table, entry = lookup(series)
    kind = entry.get(event_key(event_tail)) if isinstance(entry, dict) else entry
    if table in (AWARD_FUTURES, LEADER_FUTURES, TITLE_FUTURES, EVENT_LEADER_FUTURES):
        subject = person(row["outcome"])
        return Bet(kind=kind, team_a=None, team_b=None, subject=subject, line=None, **future) if kind and subject else None
    if series in PLAYER_LINE_FUTURES:
        subject = person(row["outcome"])
        if not subject or row["line"] is None:
            return None
        return Bet(kind=PLAYER_LINE_FUTURES[series], team_a=None, team_b=None, subject=subject, line=row["line"], **future)
    if series in LINE_FUTURES:
        m = LINE_EVENT.match(event_tail)
        subject = team(m.group(1), sport) if m else None
        if not subject or row["line"] is None:
            return None
        return Bet(kind=LINE_FUTURES[series], team_a=None, team_b=None, subject=subject, line=row["line"], **future)
    if series in TEAM_LINE_FUTURES:
        subject = team(market_tail.rstrip("0123456789"), sport)
        if not subject or row["line"] is None:
            return None
        return Bet(kind=TEAM_LINE_FUTURES[series], team_a=None, team_b=None, subject=subject, line=row["line"], **future)
    if series == CONFERENCE_SERIES:
        subject = CONFERENCE_CODES.get(market_tail)
        return Bet(kind="champion_conference", team_a=None, team_b=None, subject=subject, line=None, **future) if subject else None
    subject = team(market_tail, sport)
    if not subject:
        return None
    if series == SERIES_WINNERS:
        m = SERIES_EVENT.match(event_tail)
        kind = SERIES_ROUNDS.get(m.group(2)) if m else None
        teams = split_codes(m.group(1), sport) if kind else (None, None)
        if subject not in teams:
            return None
        team_a, team_b = sorted(teams)      # The venues list a series' teams in different orders.
        return Bet(kind=kind, team_a=team_a, team_b=team_b, subject=subject, line=None, **future)
    return Bet(kind=kind, team_a=None, team_b=None, subject=subject, line=None, **future) if kind else None


def race_side(state, market_tail, row):
    """
    Who a race's market is on, a party, 'D' or 'R', or a candidate's name key, or None: in a state whose general election
    can pit two of one party, see TOP_TWO_STATES, only a candidate.
    """
    if market_tail in PARTIES:
        return PARTIES[market_tail] if state not in TOP_TWO_STATES else None
    return person(row["outcome"])


def classify_election(row, series, event_tail, market_tail, base):
    """
    The Bet an election contract row describes, or None. Its season is the election's year.
    """
    year = event_tail.rsplit("-", 1)[-1]
    if not year.isdigit():
        return None
    election = dict(season=2000 + int(year[-2:]), game_date=None, team_a=None, team_b=None, line=None, polarity="yes", **base)
    if series in CONTROL_SERIES:
        party = PARTIES.get(market_tail)
        return Bet(kind=CONTROL_SERIES[series], subject=party, **election) if party else None
    m = HOUSE_RACE_EVENT.match(event_tail) if series == HOUSE_RACE_SERIES else None
    if m:
        state, district, kind = m.group(1), m.group(2), "house_race"
    else:
        found = next(((pattern.match(series), kind) for pattern, kind in RACE_SERIES.items() if pattern.match(series)), (None, None))
        if not found[0]:
            return None
        state, district, kind = found[0].group(1), (found[0].groups() + (None,))[1], found[1]
    if state not in STATES:
        return None
    side = race_side(state, market_tail, row)
    return Bet(kind=kind, subject=f"{race(state, district)} {side}", **election) if side else None


def ticker_date(yy, mon, dd):
    """
    The date a ticker's '26', 'SEP', '20' spell, as YYYY-MM-DD.
    """
    return datetime.strptime(f"20{yy} {mon} {dd}", "%Y %b %d").strftime("%Y-%m-%d")


def parse_game(tail, sport):
    """
    Parse a game event tail such as '26SEP20CARATL' or '26SEP291400PHIATL' into (date, first team, second team), the
    teams being the sport's.
    """
    m = GAME_DATE.match(tail)
    if not m:
        return None, None, None
    yy, mon, dd, _, pair = m.groups()
    first, second = split_codes(pair, sport)
    return ticker_date(yy, mon, dd), first, second


def classify_game(row, series, event_tail, market_tail, sport, base):
    """
    The Bet a team game's contract describes, or None. A winner's is stated as the away team winning, the home team's
    contract being its complement, and a spread's or a team total's market is the team and its rounded line, 'ATL17'
    for more than 16.5, or a total's the line alone.
    """
    game_date, away, home = parse_game(event_tail, sport)
    if not game_date or not (away and home):
        return None
    kind = GAME_SERIES[series]
    common = dict(season=season_from_date(game_date, sport), game_date=game_date, team_a=away, team_b=home, **base)
    if kind == "game_winner":
        picked = team(market_tail, sport)
        if picked not in (away, home):
            return None
        return Bet(kind=kind, subject=away, line=None, polarity="yes" if picked == away else "no", **common)
    if kind in ("spread", "team_total"):
        subject = team(market_tail.rstrip("0123456789"), sport)
        if subject not in (away, home) or row["line"] is None:
            return None
        return Bet(kind=kind, subject=subject, line=row["line"], polarity="yes", **common)
    return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common) if row["line"] is not None else None


def classify_player(row, series, event_tail, sport, base):
    """
    The Bet a player prop on one game describes, or None.
    """
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


def classify_soccer(row, series, event_tail, market_tail, sport, base):
    """
    The Bet a soccer match's contract describes, or None. The clubs are kept in order of their codes, since the bet
    names whom each market is on.
    """
    game_date, first, second = parse_game(event_tail, sport)
    if not game_date or not (first and second):
        return None
    kind = SOCCER_SERIES[series]
    common = dict(season=season_from_date(game_date, sport), game_date=game_date, team_a=min(first, second), team_b=max(first, second),
                  polarity="yes", **base)
    if kind.endswith("result"):
        subject = "tie" if market_tail == "TIE" else team(market_tail, sport)
        return Bet(kind=kind, subject=subject, line=None, **common) if subject in (first, second, "tie") else None
    if kind.endswith("spread"):
        subject = team(market_tail.rstrip("0123456789"), sport)
        return Bet(kind=kind, subject=subject, line=row["line"], **common) if subject in (first, second) and row["line"] is not None else None
    if kind.endswith("btts"):
        return Bet(kind=kind, subject=None, line=None, **common)
    if kind.endswith("exact_score"):
        m = SCORE_TAIL.match(market_tail)
        goals = {team(m.group(1), sport): m.group(2), team(m.group(3), sport): m.group(4)} if m else {}
        if set(goals) != {first, second}:
            return None
        return Bet(kind=kind, subject=" ".join(f"{club}{goals[club]}" for club in sorted(goals)), line=None, **common)
    return Bet(kind=kind, subject=None, line=row["line"], **common) if row["line"] is not None else None


def match_people(row, outcomes):
    """
    The keys of the two people a match's contract is between: from the event title when it gives their full names,
    'Natalia Silva vs. Cong Wang: Round of Victory' or '332: Silva vs Cong', else from the event's markets' subtitles.
    """
    title = re.sub(r"^\d+:\s*", "", row["event_title"] or "").split(": ", 1)[0]
    sides = match_sides(title)
    if sides:
        return sides
    names = {side_key(name) for name in outcomes.get(row["event_id"], ())} - {None}
    return tuple(names) if len(names) == 2 else None


def classify_match(row, series, event_tail, base, outcomes):
    """
    The Bet a match between two people describes, or None. The two are kept in order of their keys, and a winner's
    market is stated as the first of them winning, the other's being its complement.
    """
    m = MATCH_EVENT.match(event_tail)
    sides = match_people(row, outcomes) if m else None
    if not sides:
        return None
    game_date = ticker_date(*m.group(1, 2, 3))
    first, second = sorted(sides)
    kind = MATCH_SERIES[series]
    common = dict(season=int(game_date[:4]), game_date=game_date, team_a=first, team_b=second, **base)
    outcome = row["outcome"] or ""
    if kind in ("match_winner", "set_winner"):
        picked = side_key(outcome)
        if picked not in sides or (kind == "set_winner" and not m.group(6)):
            return None
        kind = f"set_{m.group(6)}_winner" if kind == "set_winner" else kind
        return Bet(kind=kind, subject=first, line=None, polarity="yes" if picked == first else "no", **common)
    if kind in ("games_spread", "sets_spread"):
        found = SPREAD_SIDE.match(outcome)
        picked = side_key(found.group(1)) if found else None
        return Bet(kind=kind, subject=picked, line=row["line"], polarity="yes", **common) if picked in sides and row["line"] is not None else None
    if kind == "exact_score":
        found = SCORE_SIDE.match(outcome)
        picked = side_key(found.group(1)) if found else None
        return Bet(kind=kind, subject=f"{picked} {found.group(2)}-{found.group(3)}", line=None, polarity="yes", **common) if picked in sides else None
    if kind == "round_of_victory":
        found = ROUND_SIDE.match(outcome)
        picked = side_key(found.group(1)) if found else None
        return Bet(kind=kind, subject=picked, line=float(found.group(2)), polarity="yes", **common) if picked in sides else None
    if kind == "go_the_distance":
        return Bet(kind=kind, subject=None, line=None, polarity="yes", **common)
    return Bet(kind=kind, subject=None, line=row["line"], polarity="yes", **common) if row["line"] is not None else None


def classify_race(row, series, market_tail, sport, base):
    """
    The Bet on a race's winner, or its top constructor, describes, or None. A race is its date.
    """
    found = SCHEDULED.search(row["rules"] or "")
    race_date = written_date(found.group(1)) if found else None
    kind = RACING_SERIES[series]
    subject = team(market_tail, sport) if kind == "race_constructor" else side_key(row["outcome"])
    if not race_date or not subject:
        return None
    return Bet(kind=kind, season=int(race_date[:4]), game_date=race_date, team_a=None, team_b=None, subject=subject, line=None,
               polarity="yes", **base)


def deadline(rules):
    """
    The last day a price future counts, from its rules: 'before Sep 1, 2026 at 12:00 AM ET' is August 31, and 'by Dec 31,
    2026 at 11:59 PM ET' December 31. None when the rules give none.
    """
    found = DEADLINE.search(rules or "")
    day = written_date(found.group(1)) if found else None
    return last_day(day, *found.group(2, 3, 4)) if day else None


def classify_crypto(row, series, base):
    """
    The Bet a Bitcoin contract describes, or None: a window's direction, the window being the 15 minutes before its
    close, a price going above or below its strike by a deadline, or the band it ends the year in.
    """
    if series in UPDOWN_SERIES:
        kind, coin, minutes = UPDOWN_SERIES[series]
        if not row["close_time"]:
            return None
        start = shift(row["close_time"], hours=-minutes / 60)
        return Bet(kind=kind, season=int(start[:4]), game_date=f"{start[:10]} {start[11:16]}", team_a=None, team_b=None, subject=coin,
                   line=None, polarity="yes", **base)
    if series in HIT_SERIES:
        last = deadline(row["rules"])
        if not last or row["line"] is None:
            return None
        return Bet(kind=HIT_SERIES[series], season=int(last[:4]), game_date=None, team_a=None, team_b=None, subject=last,
                   line=row["line"], polarity="yes", **base)
    band = row["outcome"] or ""
    numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", band)]
    if "or below" in band and len(numbers) == 1:
        subject = f"below {numbers[0] + 0.01:.0f}"
    elif "or above" in band and len(numbers) == 1:
        subject = f"from {numbers[0]:.0f}"
    elif " to " in band and len(numbers) == 2:
        subject = f"{numbers[0]:.0f} to {numbers[1] + 0.01:.0f}"
    else:
        return None
    year = int(row["close_time"][:4]) - 1 if row["close_time"] and row["close_time"][5:10] == "01-01" else None
    return Bet(kind=RANGE_SERIES[series], season=year, game_date=None, team_a=None, team_b=None, subject=subject, line=None,
               polarity="yes", **base) if year else None


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


def event_outcomes(rows):
    """
    Each event's markets' subtitles, {event ticker: [subtitle, ...]}, which name the people a match is between when its
    title gives only their last names.
    """
    found = defaultdict(list)
    for row in rows:
        if row["series_id"] in MATCH_SERIES:
            found[row["event_id"]].append(row["outcome"])
    return found


def classify(row, outcomes=None):
    """
    The Bet a Kalshi contract row describes, or None when it is not one we trade. outcomes is event_outcomes() of the
    venue's rows, which matches between two people need.
    """
    series, event, ticker, sport = row["series_id"], row["event_id"], row["contract_id"], row["sport"]
    event_tail = event[len(series) + 1:]
    market_tail = ticker[len(event) + 1:]
    base = dict(venue=row["venue"], contract_id=row["contract_id"])
    if series in CONTROL_SERIES or series == HOUSE_RACE_SERIES or any(p.match(series) for p in SERIES_PATTERNS):
        return classify_election(row, series, event_tail, market_tail, base)
    if series in GAME_SERIES:
        return classify_game(row, series, event_tail, market_tail, sport, base)
    if series in PLAYER_SERIES:
        return classify_player(row, series, event_tail, sport, base)
    if series in SOCCER_SERIES:
        return classify_soccer(row, series, event_tail, market_tail, sport, base)
    if series in MATCH_SERIES:
        return classify_match(row, series, event_tail, base, outcomes or {})
    if series in RACING_SERIES:
        return classify_race(row, series, market_tail, sport, base)
    if series in CRYPTO_SERIES:
        return classify_crypto(row, series, base)
    if series in FUTURE_SERIES and row["close_time"]:
        return classify_future(row, series, event_tail, market_tail, sport, base)
    return None
