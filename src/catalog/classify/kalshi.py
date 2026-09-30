"""
Turn Kalshi contracts into Bets.

The series ticker says the kind, the event ticker holds the game, and the
market ticker holds the team. College football's, hockey's, and
basketball's game series share the NFL's layout, with team codes of two
to five letters in college football, two or three in hockey, and three in
basketball. Baseball's event tickers carry the start time too, which
tells a doubleheader's two games apart. Player props name the player in
the title, before the colon.

Futures, bets on a season rather than a game, each have a series of their
own. A team future's market ticker ends in the team, an award's names the
player in its subtitle, and a season win total's event ticker ends in the
team, with one market per line. The venues number seasons differently, so
a future's season is the one its settlement falls in, which both agree
on. This is the only file that knows Kalshi's ticker layout.
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
    "KXNBAGAME": "game_winner", "KXNBASPREAD": "spread", "KXNBATOTAL": "total", "KXNBATEAMTOTAL": "team_total",
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
    "KXNBAPTS": "player_points",
    "KXNBAREB": "player_rebounds",
    "KXNBAAST": "player_assists",
    "KXNBA3PT": "player_threes",
    "KXNBABLK": "player_blocks",
}
PLAYER_TITLE = re.compile(r"^(.+?): ")     # 'Bijan Robinson: 100+ receiving yards'.

# FUTURES. Each series is one kind. The market ticker ends in the team, 'KXSB-27-KC'.
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
}
ROUND_SERIES = "KXNFLROUNDQUAL"     # Playoff round qualifiers, one event per round: 'KXNFLROUNDQUAL-27CONF'.
ROUNDS = {"CONF": "reach_conf_final", "DIV": "reach_divisional_round"}
SERIES_WINNERS = "KXMLBSERIES"      # One event per playoff series, the round last: 'KXMLBSERIES-26PHIATLWC'.
SERIES_ROUNDS = {"WC": "wild_card_series"}
SERIES_EVENT = re.compile(r"^\d{2}([A-Z]+?)(WC|DS|CS|WS)$")
# Awards, the player named in the market's subtitle, 'Aaron Judge'.
AWARD_FUTURES = {
    "KXNFLMVP": "mvp", "KXNFLOPOTY": "offensive_player", "KXNFLDPOTY": "defensive_player", "KXNFLOROTY": "offensive_rookie",
    "KXNFLDROTY": "defensive_rookie", "KXNFLCPOTY": "comeback_player", "KXNFLCOTY": "coach",
    "KXMLBALMVP": "al_mvp", "KXMLBNLMVP": "nl_mvp", "KXMLBALCY": "al_cy_young", "KXMLBNLCY": "nl_cy_young",
    "KXMLBALROTY": "al_rookie", "KXMLBNLROTY": "nl_rookie", "KXMLBWSMVP": "world_series_mvp",
    "KXNBAMVP": "mvp",
    "KXNHLHART": "hart", "KXNHLNORRIS": "norris", "KXNHLVEZINA": "vezina", "KXNHLADAMS": "jack_adams",
    "KXNHLRICHARD": "goals_leader", "KXNHLROSS": "points_leader",
    "KXHEISMAN": "heisman",
}
# Season totals, one event per team, 'KXNFLWINS-27ARI', and one market per line, stored as the strict threshold.
LINE_FUTURES = {"KXNFLWINS": "season_wins", "KXNCAAFWINS": "season_wins", "KXNBAWINS": "season_wins", "KXNHLSEASONPTS": "season_points"}
LINE_EVENT = re.compile(r"^\d{2}([A-Z]+)$")
FUTURE_SERIES = {*TEAM_FUTURES, ROUND_SERIES, SERIES_WINNERS, *AWARD_FUTURES, *LINE_FUTURES}


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


def classify_future(row, series, event_tail, market_tail, sport, base):
    """
    The Bet a futures contract row describes, or None. Its season is the one its settlement falls in.
    """
    future = dict(season=season_from_date(row["close_time"][:10], sport), game_date=None, polarity="yes", **base)
    if series in AWARD_FUTURES:
        name = row["outcome"]
        return Bet(kind=AWARD_FUTURES[series], team_a=None, team_b=None, subject=player_key(name), line=None, **future) if name else None
    if series in LINE_FUTURES:
        m = LINE_EVENT.match(event_tail)
        subject = team(m.group(1), sport) if m else None
        if not subject or row["line"] is None:
            return None
        return Bet(kind=LINE_FUTURES[series], team_a=None, team_b=None, subject=subject, line=row["line"], **future)
    subject = team(market_tail, sport)
    if not subject:
        return None
    if series == ROUND_SERIES:
        kind = ROUNDS.get(event_tail[2:])
        return Bet(kind=kind, team_a=None, team_b=None, subject=subject, line=None, **future) if kind else None
    if series == SERIES_WINNERS:
        m = SERIES_EVENT.match(event_tail)
        kind = SERIES_ROUNDS.get(m.group(2)) if m else None
        teams = split_codes(m.group(1), sport) if kind else (None, None)
        if subject not in teams:
            return None
        team_a, team_b = sorted(teams)      # The venues list a series' teams in different orders.
        return Bet(kind=kind, team_a=team_a, team_b=team_b, subject=subject, line=None, **future)
    return Bet(kind=TEAM_FUTURES[series], team_a=None, team_b=None, subject=subject, line=None, **future)


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

    if series in FUTURE_SERIES and row["close_time"]:
        return classify_future(row, series, event_tail, market_tail, sport, base)
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
