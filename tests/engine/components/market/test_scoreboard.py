"""
Tests for the scoreboard, which says which games are being played and when each one ends.
"""

import asyncio
from engine.components.market import scoreboard
from schedule import SUNDAY, event, key, schedule

GAME = ("CAR", "ATL", f"{SUNDAY}T17:00:00+00:00")


def board(tmp_path, states=lambda events: {}, logs=None):
    return scoreboard.Scoreboard(schedule(tmp_path, [GAME]), ("nfl",), (logs if logs is not None else []).append, states)


def test_a_game_the_venue_says_nothing_about_is_in_play_for_its_expected_length(tmp_path):
    b = board(tmp_path)
    assert b.games == {key(*GAME): (GAME[2], event(*GAME))}         # Polymarket US gives the kickoff, and its event.
    assert not b.in_play(key(*GAME), f"{SUNDAY}T16:59:00+00:00")
    assert b.in_play(key(*GAME), GAME[2]) and b.in_play(key(*GAME), f"{SUNDAY}T20:14:00+00:00")
    assert not b.in_play(key(*GAME), f"{SUNDAY}T20:15:00+00:00")                 # Kickoff plus the expected 3.25 hours.
    assert b.end(key(*GAME), f"{SUNDAY}T18:00:00+00:00") == f"{SUNDAY}T20:15:00+00:00"
    assert b.settles(key(*GAME), f"{SUNDAY}T18:00:00+00:00") == f"{SUNDAY}T20:45:00+00:00"
    assert not b.in_play(("nfl", SUNDAY, "X", "Y"), GAME[2])                     # A game the catalog does not pair.


def test_a_game_the_venue_calls_over_stops_at_its_real_end(tmp_path):
    logs = []
    b = board(tmp_path, lambda events: {event(*GAME): {"live": False, "ended": True, "finished": f"{SUNDAY}T19:58:00+00:00"}}, logs)
    asyncio.run(b.check(f"{SUNDAY}T20:00:00+00:00"))
    assert not b.in_play(key(*GAME), f"{SUNDAY}T20:00:00+00:00")
    assert b.end(key(*GAME), f"{SUNDAY}T20:00:00+00:00") == f"{SUNDAY}T19:58:00+00:00"
    assert b.settles(key(*GAME), f"{SUNDAY}T20:00:00+00:00") == f"{SUNDAY}T20:28:00+00:00"     # Its money is expected back sooner.
    assert logs == ["game over: nfl 2026-09-27 CAR@ATL, 2.97 hours after kickoff"]
    assert b.under_way(f"{SUNDAY}T20:01:00+00:00") == {}                                        # Not asked about again.


def test_a_game_the_venue_says_is_live_is_played_past_its_expected_length(tmp_path):
    b = board(tmp_path, lambda events: {event(*GAME): {"live": True, "ended": False, "finished": None}})
    asyncio.run(b.check(f"{SUNDAY}T20:20:00+00:00"))
    assert b.in_play(key(*GAME), f"{SUNDAY}T20:20:30+00:00")
    assert b.end(key(*GAME), f"{SUNDAY}T20:20:30+00:00") == f"{SUNDAY}T20:20:30+00:00"         # It may end at any moment.
    # A live reading holds for three lookups, 90 seconds, and then the expected length rules again.
    assert b.in_play(key(*GAME), f"{SUNDAY}T20:21:30+00:00")
    assert not b.in_play(key(*GAME), f"{SUNDAY}T20:21:31+00:00")


def test_a_failed_lookup_is_logged_and_the_game_keeps_its_expected_length(tmp_path):
    def down(events):
        raise OSError("gateway down")
    logs = []
    b = board(tmp_path, down, logs)
    asyncio.run(b.check(f"{SUNDAY}T18:00:00+00:00"))
    assert logs[0].startswith("scoreboard lookup failed (OSError('gateway down')), will retry")
    assert b.in_play(key(*GAME), f"{SUNDAY}T18:00:00+00:00") and not b.in_play(key(*GAME), f"{SUNDAY}T20:15:00+00:00")


def test_only_games_under_way_are_asked_about_and_only_when_a_lookup_is_due(tmp_path):
    asked = []
    b = board(tmp_path, lambda events: asked.append(events) or {})
    asyncio.run(b.check(f"{SUNDAY}T16:00:00+00:00"))            # Before kickoff.
    asyncio.run(b.check(f"{SUNDAY}T22:01:00+00:00"))            # Past the five hours its books are recorded for.
    assert asked == []

    async def ticks():
        b.tick(f"{SUNDAY}T18:00:00+00:00", 1000.0)
        await b.lookups.running
        b.tick(f"{SUNDAY}T18:00:10+00:00", 1010.0)             # Ten seconds on, too soon for another.
        await b.lookups.running
    asyncio.run(ticks())
    assert asked == [[event(*GAME)]]
