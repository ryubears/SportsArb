"""
Every table of sport specific settings covers every sport the catalog fetches, and nothing else,
every list of sports names only those, every sport with player props has their rules note, and the
Kalshi series fetched are the ones the Kalshi classifier reads.
"""

import pytest
from catalog import fetch, match
from catalog.classify import kalshi, polymarket_us, teams
from common import timeutil
from engine.helper import config


@pytest.mark.parametrize("name, table", [
    ("config.GAME_HOURS", config.GAME_HOURS),
    ("config.DOLLARS_PER_CAP_HOUR", config.DOLLARS_PER_CAP_HOUR),
    ("teams.ALIASES, the files in classify/aliases/", teams.ALIASES),
    ("polymarket_us.EVENT_PREFIX", polymarket_us.EVENT_PREFIX),
    ("match.KIND_NOTES", match.KIND_NOTES),
])
def test_table_covers_every_sport(name, table):
    assert set(table) == set(fetch.SPORTS), name


@pytest.mark.parametrize("name, sports", [
    ("config.LIVE_SPORTS", config.LIVE_SPORTS),
    ("timeutil.CALENDAR_SEASONS", timeutil.CALENDAR_SEASONS),
])
def test_sports_named_apart_are_ones_the_catalog_fetches(name, sports):
    assert set(sports) <= set(fetch.SPORTS), name


def test_every_sport_whose_player_props_are_fetched_has_their_rules_note():
    with_props = {sport for sport, venues in fetch.SPORTS.items() if set(venues["kalshi"]["tickers"]) & set(kalshi.PLAYER_SERIES)}
    assert with_props <= set(match.PLAYER_NOTES) <= set(fetch.SPORTS)
    assert "nhl" not in with_props      # Left out, see fetch.SPORTS.


def test_the_kalshi_series_fetched_are_the_ones_the_kalshi_classifier_reads():
    fetched = [ticker for venues in fetch.SPORTS.values() for ticker in venues["kalshi"]["tickers"]]
    assert sorted(fetched) == sorted({**kalshi.GAME_SERIES, **kalshi.PLAYER_SERIES})
