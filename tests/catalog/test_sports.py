"""
Every table of sport specific settings covers every sport the catalog fetches, and nothing else.
"""

import pytest
from catalog import fetch
from catalog.classify import polymarket_us, teams
from engine.helper import config


@pytest.mark.parametrize("name, table", [
    ("config.GAME_HOURS", config.GAME_HOURS),
    ("teams.ALIASES, the files in classify/aliases/", teams.ALIASES),
    ("polymarket_us.EVENT_PREFIX", polymarket_us.EVENT_PREFIX),
])
def test_table_covers_every_sport(name, table):
    assert set(table) == set(fetch.SPORTS), name
