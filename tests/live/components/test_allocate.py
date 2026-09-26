"""
Tests for the capital allocator on a Sunday schedule.
"""

from db import database
from db.models import Bet, Contract, Pair, Trade
from live.components import allocate, balances
from live.helper import config

SUNDAY = "2026-09-27"
EARLY = [(f"E{i}", f"H{i}", f"{SUNDAY}T17:00:00+00:00") for i in range(9)]
LATE = [("L0", "M0", f"{SUNDAY}T20:05:00+00:00"), ("L1", "M1", f"{SUNDAY}T20:05:00+00:00"),
        ("L2", "M2", f"{SUNDAY}T20:25:00+00:00"), ("L3", "M3", f"{SUNDAY}T20:25:00+00:00")]
NIGHT = [("S", "N", "2026-09-28T00:20:00+00:00")]


def schedule(tmp_path, games):
    """
    A database with one game winner pair per game, each with a Kalshi and a Polymarket US contract kicking off at the given time.
    """
    conn = database.connect(tmp_path / "t.sqlite")
    contracts, bets, pairs = [], [], []
    for away, home, kickoff in games:
        members = []
        for venue in ("kalshi", "polymarket_us"):
            cid = f"{venue}-{away}{home}"
            contracts.append(Contract(venue=venue, contract_id=cid, market_id=cid, event_id="e", series_id=None, sport="nfl", event_title=None,
                                      title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=kickoff,
                                      close_time=kickoff, fee_info=None))
            members.append(Bet(venue, cid, "game_winner", 2027, kickoff[:10], away, home, away, None, "yes"))
        bets += members
        pairs.append(Pair(f"game_winner {kickoff[:10]} {away}@{home} {away}", "game_winner", 2027, kickoff[:10], away, home, away, None, members, []))
    database.upsert_contracts(conn, contracts, "2026-09-25T00:00:00+00:00")
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", pairs, "2026-09-25T00:00:00+00:00")
    return conn


def pair_for(away, home, kickoff):
    return {"game_date": kickoff[:10], "team_a": away, "team_b": home}


def test_caps_follow_the_active_games(tmp_path):
    conn = schedule(tmp_path, EARLY + LATE + NIGHT)
    allocator = allocate.Allocator(conn, balances.Balances(conn))
    early, late, night = EARLY[0], LATE[0], NIGHT[0]
    # Nine early games share each venue's 10,000, and the cap holds through the game while the same nine are active.
    assert allocator.cap(pair_for(*early), f"{SUNDAY}T17:00:00+00:00") == int(10000 / 9 / 20)
    assert allocator.cap(pair_for(*early), f"{SUNDAY}T19:00:00+00:00") == int(10000 / 9 / 20)
    # A late game kicking off at 20:05 shares with the nine early games still out and the other 20:05 game, eleven in all.
    assert allocator.cap(pair_for(*late), f"{SUNDAY}T20:05:00+00:00") == int(10000 / 11 / 20)
    # Once the early games have settled at 20:45, the four late games have the pool.
    assert allocator.cap(pair_for(*late), f"{SUNDAY}T21:00:00+00:00") == int(10000 / 4 / 20)
    # The night game is alone.
    assert allocator.cap(pair_for(*night), "2026-09-28T01:00:00+00:00") == config.MAX_CAP
    # Nothing before kickoff, after the final whistle, or for a bet with no game.
    assert allocator.cap(pair_for(*early), f"{SUNDAY}T16:59:00+00:00") == 0
    assert allocator.cap(pair_for(*early), f"{SUNDAY}T20:16:00+00:00") == 0
    assert allocator.cap({"game_date": None}, f"{SUNDAY}T17:00:00+00:00") == 0
    assert allocator.summary(f"{SUNDAY}T18:00:00+00:00") == "capital: 9 games in play or settling, 1,111$ a venue each, cap 55"
    assert allocator.summary(f"{SUNDAY}T23:00:00+00:00") == "capital: 4 games in play or settling, 2,500$ a venue each, cap 125"
    assert allocator.summary("2026-09-28T05:00:00+00:00") == "capital: no games in play"


def test_money_a_game_holds_stays_in_the_pool_and_a_game_past_its_share_stops(tmp_path):
    conn = schedule(tmp_path, EARLY[:2])
    cash = balances.Balances(conn)
    allocator = allocate.Allocator(conn, cash)
    (first, second), pairs = EARLY[:2], database.load_pairs(conn, "nfl")
    first_pair = next(p for p in pairs.values() if p["team_a"] == "E0")
    t = Trade(pair_id=first_pair["id"], trade="t", signal_ts=f"{SUNDAY}T17:00:00+00:00", edge=0.05, quantity=100,
              yes_venue="kalshi", yes_contract="kalshi-E0H0", yes_polarity="yes", yes_limit=0.5,
              no_venue="polymarket_us", no_contract="polymarket_us-E0H0", no_polarity="yes", no_limit=0.45, pays_at=f"{SUNDAY}T21:00:00+00:00",
              yes_filled=100, yes_cost=4000.0, no_filled=100, no_cost=4000.0, yes_held=100, no_held=100, matched=100, status="filled")
    database.insert_trade(conn, t)
    cash.amounts = {"kalshi": 6000.0, "polymarket_us": 6000.0}          # After paying for it.
    now = f"{SUNDAY}T17:30:00+00:00"
    assert allocator.deployed() == {(SUNDAY, "E0", "H0"): {"kalshi": 4000.0, "polymarket_us": 4000.0}}
    # The pool is still 10,000 a venue and each game's share 5,000, so both caps stay at 250.
    assert allocator.cap(first_pair, now) == allocator.cap(pair_for(*second), now) == 250
    # Spent past its share, the first game gets nothing more while the second is unaffected.
    t.yes_cost = t.no_cost = 5500.0
    database.update_trade(conn, t)
    cash.amounts = {"kalshi": 4500.0, "polymarket_us": 4500.0}
    assert allocator.cap(first_pair, now) == 0
    assert allocator.cap(pair_for(*second), now) == 250
