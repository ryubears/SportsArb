"""
Tests for the game timing shared by the scanner, executor, allocator, and recorder.
"""

from engine.helper import game

KICKOFF = "2026-09-20T17:00:00+00:00"
GAME = {"start_time": KICKOFF, "close_time": "2026-09-20T17:00:00+00:00"}
FUTURE = {"start_time": None, "close_time": "2027-02-14T00:00:00+00:00"}
UNKNOWN = {"start_time": None, "close_time": None}


def test_a_game_is_in_play_until_the_whistle_then_settling():
    assert not game.in_play(KICKOFF, "2026-09-20T16:59:59+00:00", "nfl")
    assert game.in_play(KICKOFF, KICKOFF, "nfl")
    assert not game.in_play(KICKOFF, "2026-09-20T20:15:00+00:00", "nfl")
    assert game.in_play_or_settling(KICKOFF, "2026-09-20T20:15:00+00:00", "nfl")
    assert not game.in_play_or_settling(KICKOFF, "2026-09-20T20:45:00+00:00", "nfl")


def test_how_long_a_game_lasts_is_the_sports_own(monkeypatch):
    monkeypatch.setitem(game.config.GAME_HOURS, "nba", 2.5)
    assert game.in_play(KICKOFF, "2026-09-20T19:00:00+00:00", "nfl") and not game.in_play(KICKOFF, "2026-09-20T19:30:00+00:00", "nba")
    assert game.pays_at([GAME], "nba") == "2026-09-20T20:00:00+00:00"


def test_payout_is_the_slowest_member_and_skips_members_without_times():
    assert game.pays_at([GAME], "nfl") == "2026-09-20T20:45:00+00:00"
    assert game.pays_at([GAME, FUTURE], "nfl") == FUTURE["close_time"]
    assert game.pays_at([GAME, UNKNOWN], "nfl") == "2026-09-20T20:45:00+00:00"
    assert game.pays_at([UNKNOWN], "nfl") is None
    assert game.kickoff([FUTURE, GAME]) == KICKOFF
    assert game.kickoff([FUTURE]) is None
