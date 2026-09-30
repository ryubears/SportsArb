"""
A database of games for the scoreboard tests. Each game has
one game winner pair, with a Kalshi contract, which gives no kickoff, and
a Polymarket US one, which gives the kickoff and the event its state is
read from, as the venues do.
"""

from db import database
from db.models import Bet, Contract, Pair

SUNDAY = "2026-09-27"
EARLY = [(f"E{i}", f"H{i}", f"{SUNDAY}T17:00:00+00:00") for i in range(9)]
LATE = [("L0", "M0", f"{SUNDAY}T20:05:00+00:00"), ("L1", "M1", f"{SUNDAY}T20:05:00+00:00"),
        ("L2", "M2", f"{SUNDAY}T20:25:00+00:00"), ("L3", "M3", f"{SUNDAY}T20:25:00+00:00")]
NIGHT = [("S", "N", "2026-09-28T00:20:00+00:00")]
LONDON = [("LO", "ND", f"{SUNDAY}T13:30:00+00:00")]


def event(away, home, kickoff, sport="nfl"):
    """
    The Polymarket US event a game is played in.
    """
    return f"{sport}-{away}-{home}-{kickoff[:10]}"


def key(away, home, kickoff, sport="nfl"):
    """
    The game key the scoreboard uses.
    """
    return (sport, kickoff[:10], away, home)


def pair_for(away, home, kickoff, sport="nfl"):
    """
    The fields of a pair that say which game it is on.
    """
    return {"sport": sport, "game_date": kickoff[:10], "team_a": away, "team_b": home}


def add_games(conn, games, sport="nfl"):
    """
    Store the games, each as (away, home, kickoff), as the pairs of their sport.
    """
    contracts, bets, pairs = [], [], []
    for away, home, kickoff in games:
        members = []
        for venue in ("kalshi", "polymarket_us"):
            cid = f"{venue}-{away}{home}"
            gives_kickoff = venue == "polymarket_us"
            contracts.append(Contract(venue=venue, contract_id=cid, market_id=cid, event_id=event(away, home, kickoff, sport) if gives_kickoff else "k",
                                      series_id=None, sport=sport, event_title=None, title="t", outcome="Yes", market_type=None, line=None,
                                      rules=None, start_time=kickoff if gives_kickoff else None, close_time=kickoff, fee_info=None))
            members.append(Bet(venue, cid, "game_winner", 2027, kickoff[:10], away, home, away, None, "yes"))
        bets += members
        pairs.append(Pair(f"{sport} game_winner {kickoff[:10]} {away}@{home} {away}", "game_winner", 2027, kickoff[:10], away, home, away, None,
                          members, [], sport=sport))
    database.upsert_contracts(conn, contracts, "2026-09-25T00:00:00+00:00")
    database.replace_bets(conn, sport, bets)
    database.replace_pairs(conn, sport, pairs, "2026-09-25T00:00:00+00:00")


def schedule(tmp_path, games, sport="nfl"):
    """
    A database holding the games, all of one sport.
    """
    conn = database.connect(tmp_path / "t.sqlite")
    add_games(conn, games, sport)
    return conn
