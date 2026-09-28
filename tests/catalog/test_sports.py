"""
Every table of sport specific settings covers every sport the catalog fetches, and nothing else,
and the Kalshi series fetched are the ones the Kalshi classifier reads.
"""

import pytest
from catalog import fetch
from catalog.classify import kalshi, polymarket_us, teams
from engine.helper import config


@pytest.mark.parametrize("name, table", [
    ("config.GAME_HOURS", config.GAME_HOURS),
    ("config.DOLLARS_PER_CAP_HOUR", config.DOLLARS_PER_CAP_HOUR),
    ("teams.ALIASES, the files in classify/aliases/", teams.ALIASES),
    ("polymarket_us.EVENT_PREFIX", polymarket_us.EVENT_PREFIX),
])
def test_table_covers_every_sport(name, table):
    assert set(table) == set(fetch.SPORTS), name


def test_the_kalshi_series_fetched_are_the_ones_the_kalshi_classifier_reads():
    fetched = [ticker for venues in fetch.SPORTS.values() for ticker in venues["kalshi"]["tickers"]]
    assert sorted(fetched) == sorted({**kalshi.GAME_SERIES, **kalshi.PLAYER_SERIES})
