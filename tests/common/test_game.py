"""
Tests for the game timing shared by the scanner, executor, allocator, and recorder.
"""

from common import game

KICKOFF = "2026-09-20T17:00:00+00:00"
GAME = {"start_time": KICKOFF, "close_time": "2026-09-20T17:00:00+00:00"}
FUTURE = {"start_time": None, "close_time": "2027-02-14T00:00:00+00:00"}
UNKNOWN = {"start_time": None, "close_time": None}


def test_a_game_is_in_play_until_the_whistle_then_settling():
    assert not game.in_play(KICKOFF, "2026-09-20T16:59:59+00:00")
    assert game.in_play(KICKOFF, KICKOFF)
    assert not game.in_play(KICKOFF, "2026-09-20T20:15:00+00:00")
    assert game.in_play_or_settling(KICKOFF, "2026-09-20T20:15:00+00:00")
    assert not game.in_play_or_settling(KICKOFF, "2026-09-20T20:45:00+00:00")


def test_payout_is_the_slowest_member_and_skips_members_without_times():
    assert game.pays_at([GAME]) == "2026-09-20T20:45:00+00:00"
    assert game.pays_at([GAME, FUTURE]) == FUTURE["close_time"]
    assert game.pays_at([GAME, UNKNOWN]) == "2026-09-20T20:45:00+00:00"
    assert game.pays_at([UNKNOWN]) is None
    assert game.kickoff([FUTURE, GAME]) == KICKOFF
    assert game.kickoff([FUTURE]) is None
