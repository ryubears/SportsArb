"""
Tests for the notes on where the venues' rules differ.
"""

from catalog import notes


def test_a_pair_carries_its_kinds_note_its_sports_player_note_or_an_awards():
    assert notes.for_pair("mlb", "total") == [notes.BASEBALL_POSTPONED]
    assert notes.for_pair("nba", "player_points") == [notes.PLAYER_NOTES["nba"]]
    assert notes.for_pair("nhl", "hart") == [notes.AWARD_NOTE]


def test_a_kind_the_venues_settle_alike_carries_none():
    assert notes.for_pair("nfl", "champion") == [] and notes.for_pair("nfl", "season_wins") == []
