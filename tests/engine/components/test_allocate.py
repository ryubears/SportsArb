"""
Tests for the allocator's half hour plans, on a Sunday's schedule.

Paper money is 10,000 dollars a venue less the 500 floor, so 9,500 can be
spent, and an NFL game is expected to spend RATE dollars an hour for every
contract of cap over its 3.25 hours, holding it until half an hour after.
"""

import asyncio
import pytest
from db import database
from db.models import Trade
from engine.components import allocate
from engine.components.balance.live import LiveBalances
from engine.components.balance.paper import PaperBalances
from engine.components.scoreboard import Scoreboard
from engine.helper import config
from schedule import EARLY, LATE, LONDON, NIGHT, SUNDAY, event, key, pair_for, schedule

RATE = config.DOLLARS_PER_CAP_HOUR["nfl"]
GAME = RATE * 3.25          # What a contract of cap spends over a whole game.
FREE = 10000 - 500


def plan_for(tmp_path, games, cash=None, states=lambda events: {}):
    """
    An allocator of paper money, or of the cash given, over the games.
    """
    conn = schedule(tmp_path, games)
    return conn, allocate.Allocator(conn, cash(conn) if cash else PaperBalances(conn), Scoreboard(conn, ("nfl",), lambda m: None, states))


def holding(conn, game, dollars, signal_ts, status="filled"):
    """
    Store a trade on the game holding dollars on each venue.
    """
    pair = next(p for p in database.load_pairs(conn, "nfl").values() if p["team_a"] == game[0])
    t = Trade(mode="paper", pair_id=pair["id"], trade="t", signal_ts=signal_ts, edge=0.05, quantity=int(dollars * 2),
              yes_venue="kalshi", yes_contract=f"kalshi-{game[0]}{game[1]}", yes_polarity="yes", yes_limit=0.5,
              no_venue="polymarket_us", no_contract=f"polymarket_us-{game[0]}{game[1]}", no_polarity="yes", no_limit=0.5,
              pays_at=f"{SUNDAY}T21:00:00+00:00", status=status)
    if status != "sent":
        t.yes_filled = t.no_filled = t.yes_held = t.no_held = t.matched = int(dollars * 2)
        t.yes_cost = t.no_cost = dollars
    database.insert_trade(conn, t)


def test_a_game_alone_may_spend_all_the_money_over_its_play(tmp_path):
    conn, allocator = plan_for(tmp_path, EARLY[:1])
    now = EARLY[0][2]
    assert allocator.cap(pair_for(*EARLY[0]), now) == int(FREE / GAME)                             # 942.
    assert allocator.room(now) == pytest.approx({venue: FREE / GAME * RATE * 0.5 for venue in ("kalshi", "polymarket_us")})


def test_the_early_games_leave_room_for_the_games_that_kick_off_before_their_money_is_back(tmp_path):
    now = EARLY[0][2]
    _, alone = plan_for(tmp_path / "alone", EARLY)
    _, sunday = plan_for(tmp_path / "sunday", EARLY + LATE + NIGHT)
    # Alone, the nine share the money. With the late games, which play 40 and 20 minutes before the early money is back at
    # 20:45, part of it waits for them. The night game kicks off after everything else has settled and changes nothing.
    assert alone.cap(pair_for(*EARLY[0]), now) == int(FREE / (9 * GAME)) == 104
    assert sunday.cap(pair_for(*EARLY[0]), now) == int(FREE / (9 * GAME + RATE * (2 * 40 + 2 * 20) / 60)) == 98


def test_a_game_that_is_over_before_the_crowd_arrives_gets_a_bigger_cap(tmp_path):
    # London at 9:30, then eight games at 1:00 that kick off 15 minutes before London's money is back.
    conn, allocator = plan_for(tmp_path, LONDON + EARLY[:8])
    now = LONDON[0][2]
    early = FREE / (8 * GAME)                               # The eight are held back by their own crowd at 20:45.
    london = (FREE - 8 * RATE * 0.25 * early) / GAME        # London leaves the eight their first 15 minutes at that cap.
    assert allocator.current(now).caps[key(*EARLY[0])] == pytest.approx(early)
    assert allocator.cap(pair_for(*LONDON[0]), now) == int(london) == 870
    assert allocator.cap(pair_for(*EARLY[0]), now) == 0                    # Not in play yet.


def test_money_a_settling_game_holds_counts_from_when_it_comes_back(tmp_path):
    conn, allocator = plan_for(tmp_path, EARLY + LATE)
    holding(conn, EARLY[0], 8000.0, f"{SUNDAY}T17:30:00+00:00")
    allocator.cash.amounts = {"kalshi": 2000.0, "polymarket_us": 2000.0}   # After paying for it.
    now = f"{SUNDAY}T20:30:00+00:00"
    # The 1,500 free now covers the late games until 20:45, when the 8,000 comes back, and all 9,500 covers them
    # to 23:50, when the first two settle: they play 170 minutes from now and the other two 190.
    assert allocator.cap(pair_for(*LATE[0]), now) == int(FREE / (RATE * (2 * 170 + 2 * 190) / 60)) == 255


def test_the_half_hours_budget_is_shared_by_every_game_and_counts_the_trades_since_the_plan(tmp_path):
    conn, allocator = plan_for(tmp_path, EARLY)
    budget = FREE / (9 * GAME) * 9 * RATE * 0.5
    holding(conn, EARLY[0], 999.0, f"{SUNDAY}T16:59:00+00:00")           # Before the plan, so it does not count.
    now = EARLY[0][2]
    assert allocator.room(now) == pytest.approx({"kalshi": budget, "polymarket_us": budget})
    holding(conn, EARLY[0], 1000.0, f"{SUNDAY}T17:05:00+00:00")
    holding(conn, EARLY[1], 200.0, f"{SUNDAY}T17:06:00+00:00", status="sent")     # In flight, at what its orders may pay.
    assert allocator.room(f"{SUNDAY}T17:10:00+00:00") == pytest.approx({"kalshi": budget - 1200, "polymarket_us": budget - 1200})
    holding(conn, EARLY[2], 500.0, f"{SUNDAY}T17:11:00+00:00")
    assert allocator.room(f"{SUNDAY}T17:12:00+00:00") == {"kalshi": 0.0, "polymarket_us": 0.0}
    # The next half hour plans again, from what is free then, with nothing spent against it yet.
    assert allocator.current(f"{SUNDAY}T17:30:00+00:00").made == f"{SUNDAY}T17:30:00+00:00"
    assert min(allocator.room(f"{SUNDAY}T17:30:00+00:00").values()) > 0
    assert allocator.summary(f"{SUNDAY}T17:30:00+00:00").startswith("paper capital: 9 games in play and 0 more within 24 hours, ")


def test_nothing_before_kickoff_after_the_venue_calls_the_game_over_or_for_a_bet_with_no_game(tmp_path):
    ended = {"live": False, "ended": True, "finished": f"{SUNDAY}T19:50:00+00:00"}
    conn, allocator = plan_for(tmp_path, EARLY[:1], states=lambda events: {event(*EARLY[0]): ended})
    assert allocator.cap(pair_for(*EARLY[0]), f"{SUNDAY}T16:59:00+00:00") == 0
    asyncio.run(allocator.scoreboard.check(f"{SUNDAY}T19:55:00+00:00"))
    assert allocator.cap(pair_for(*EARLY[0]), f"{SUNDAY}T19:55:00+00:00") == 0
    assert allocator.cap({"sport": "nfl", "game_date": None}, f"{SUNDAY}T17:00:00+00:00") == 0
    assert allocator.summary(f"{SUNDAY}T19:55:00+00:00") == "paper capital: no games in play, 0 within 24 hours"


def test_live_caps_stay_within_the_live_bounds_and_the_budget(tmp_path):
    def live(reading):
        def make(conn):
            cash = LiveBalances(lambda m: None, {"kalshi": lambda: reading, "polymarket_us": lambda: reading})
            asyncio.run(cash.refresh(f"{SUNDAY}T16:00:00+00:00"))
            return cash
        return make

    now = EARLY[0][2]
    _, rich = plan_for(tmp_path / "rich", EARLY, live(5000.0))
    assert rich.cap(pair_for(*EARLY[0]), now) == config.LIVE_MAX_CAP
    _, test = plan_for(tmp_path / "test", EARLY, live(50.0))
    share = (50 - config.LIVE_CASH_FLOOR) / (9 * GAME)                     # Half a contract.
    assert test.cap(pair_for(*EARLY[0]), now) == config.LIVE_MIN_CAP        # Raised to one contract,
    assert test.room(now) == pytest.approx({venue: share * 9 * RATE * 0.5 for venue in ("kalshi", "polymarket_us")})     # while 7 dollars last.
    _, paper = plan_for(tmp_path / "paper", EARLY, lambda conn: PaperBalances(conn, start=600.0))
    assert paper.cap(pair_for(*EARLY[0]), now) == 0                        # 100 over the floor gives 1, under paper's least.


def test_a_plan_made_before_the_live_balances_are_read_is_not_kept(tmp_path):
    conn, allocator = plan_for(tmp_path, EARLY, lambda conn: LiveBalances(lambda m: None, {"kalshi": lambda: 100.0, "polymarket_us": lambda: 100.0}))
    now = EARLY[0][2]
    assert allocator.room(now) == {"kalshi": 0.0, "polymarket_us": 0.0} and allocator.plan is None
    asyncio.run(allocator.cash.refresh(now))
    assert min(allocator.room(now).values()) > 0 and allocator.plan is not None


def test_a_catalog_refresh_plans_again_at_once(tmp_path):
    conn, allocator = plan_for(tmp_path, EARLY)
    first = allocator.current(EARLY[0][2])
    allocator.reload()
    assert allocator.current(f"{SUNDAY}T17:10:00+00:00") is not first
    assert allocator.plan.made == f"{SUNDAY}T17:10:00+00:00" and allocator.plan.ends == f"{SUNDAY}T17:30:00+00:00"
