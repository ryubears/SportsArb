"""
Turn Kalshi contracts into Bets.

Only futures are cataloged, bets on a season, a title, an award, a
season's leader, or an election, never on one game. Each kind has a series
of its own, or an event of its own within a series, 'KXEPLTOP-27TOP4' the
top four of the Premier League, which the event's key names once its year
is taken out. A team future's market ticker ends in the team, 'KXSB-27-KC'.
An award's, a leader's, or a title holder's names the person in its
subtitle, 'Aaron Judge'. A team's season total has one event per team,
'KXNFLWINS-27ARI', and one market per line, and a player's has one event
per line and one market per player. The venues number seasons
differently, so a future's season is the one its settlement falls in,
which both agree on.

Elections have a series per office and state, 'SENATEGA', one event per
election year, '-26', and a market per party, D or R, or per candidate.
A race's season is its election year. In a state whose general election
can put two of one party on the ballot, California's and Washington's top
two and Alaska's top four, the venues read a party's win differently, so
there only the candidates are paired. This is the only file that knows
Kalshi's ticker layout.
"""

import re
from catalog.classify.teams import STATES, TOP_TWO_STATES, person, race, team_from_code
from common.timeutil import season_from_date
from db.models import Bet

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


def classify(row):
    """
    The Bet a Kalshi contract row describes, or None when it is not one we trade.
    """
    series, event, ticker, sport = row["series_id"], row["event_id"], row["contract_id"], row["sport"]
    event_tail = event[len(series) + 1:]
    market_tail = ticker[len(event) + 1:]
    base = dict(venue=row["venue"], contract_id=row["contract_id"])
    if series in CONTROL_SERIES or series == HOUSE_RACE_SERIES or any(p.match(series) for p in SERIES_PATTERNS):
        return classify_election(row, series, event_tail, market_tail, base)
    if series in FUTURE_SERIES and row["close_time"]:
        return classify_future(row, series, event_tail, market_tail, sport, base)
    return None
