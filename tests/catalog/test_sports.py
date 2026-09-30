"""
Every table of sport specific settings covers every sport the catalog fetches, and nothing else,
every list of sports names only those, the Kalshi series fetched are the ones the Kalshi classifier reads,
and every award the classifiers read carries the award note.
"""

import pytest
from catalog import fetch, match
from catalog.classify import kalshi, polymarket_us, teams
from common import timeutil
from engine.helper import config


@pytest.mark.parametrize("name, table", [
    ("config.GAME_HOURS", config.GAME_HOURS),
    ("teams.ALIASES, the files in classify/aliases/", teams.ALIASES),
    ("polymarket_us.EVENT_PREFIX", polymarket_us.EVENT_PREFIX),
    ("match.KIND_NOTES", match.KIND_NOTES),
    ("match.PLAYER_NOTES", match.PLAYER_NOTES),
])
def test_table_covers_every_sport(name, table):
    assert set(table) == set(fetch.SPORTS), name


@pytest.mark.parametrize("name, sports", [
    ("timeutil.CALENDAR_SEASONS", timeutil.CALENDAR_SEASONS),
])
def test_sports_named_apart_are_ones_the_catalog_fetches(name, sports):
    assert set(sports) <= set(fetch.SPORTS), name


def test_the_kalshi_series_fetched_are_the_ones_the_kalshi_classifier_reads():
    fetched = [ticker for venues in fetch.SPORTS.values() for ticker in venues["kalshi"]["tickers"]]
    assert sorted(fetched) == sorted({*kalshi.GAME_SERIES, *kalshi.PLAYER_SERIES, *kalshi.FUTURE_SERIES})


def test_every_award_the_classifiers_read_carries_the_award_note():
    awards = {*kalshi.AWARD_FUTURES.values(), *polymarket_us.AWARD_FUTURES.values()}
    assert awards == {kind for kind, note in match.FUTURE_NOTES.items() if note == match.AWARD_NOTE}
