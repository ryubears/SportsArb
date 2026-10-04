"""
Tests for the notes on where the venues' rules differ.
"""

from catalog import notes


def test_a_pair_carries_its_kinds_note():
    assert notes.for_pair("nhl", "hart") == [notes.AWARD_NOTE]
    assert notes.for_pair("nfl", "sacks_leader") == [notes.LEADER_NOTE]
    assert notes.for_pair("ufc", "lightweight_champion") == [notes.UFC_NOTE]
    assert notes.for_pair("politics", "house_race") == [notes.ELECTION_NOTE]


def test_a_kind_the_venues_settle_alike_carries_none():
    assert notes.for_pair("nfl", "champion") == [] and notes.for_pair("nfl", "season_wins") == []
