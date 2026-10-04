"""
Every table of sport specific settings covers every sport the catalog fetches, and nothing else,
every list of sports names only those, the Kalshi series fetched are the ones the Kalshi classifier reads,
and every award and leader the classifiers read carries its note.
"""

import pytest
from catalog import fetch, notes
from catalog.classify import kalshi, polymarket_us, teams
from common import timeutil
from engine.helper import config


@pytest.mark.parametrize("name, table", [
    ("teams.ALIASES, the files in classify/aliases/", teams.ALIASES),
    ("polymarket_us.EVENT_PREFIX", polymarket_us.EVENT_PREFIX),
    ("config.GAME_HOURS", config.GAME_HOURS),
    ("fetch.GAME_PREFIXES, with politics, which has no game", {**fetch.GAME_PREFIXES, "politics": ()}),
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
    events = {*kalshi.GAME_SERIES, *kalshi.PLAYER_SERIES, *kalshi.SOCCER_SERIES, *kalshi.MATCH_SERIES, *kalshi.RACING_SERIES,
              *kalshi.CRYPTO_SERIES}
    assert sorted(fetched) == sorted({*kalshi.FUTURE_SERIES, *events})
    patterns = [p for venues in fetch.SPORTS.values() for p in venues["kalshi"].get("patterns", ())]
    assert sorted(p.pattern for p in patterns) == sorted(p.pattern for p in kalshi.SERIES_PATTERNS)


def test_no_two_sports_fetch_one_kalshi_series_or_one_polymarket_us_prefix():
    fetched = [ticker for venues in fetch.SPORTS.values() for ticker in venues["kalshi"]["tickers"]]
    prefixes = [p for sport_prefixes in polymarket_us.EVENT_PREFIX.values() for p in sport_prefixes]
    assert len(fetched) == len(set(fetched)) and len(prefixes) == len(set(prefixes))


def test_every_award_and_leader_the_classifiers_read_carries_its_note():
    awards = {*kalshi.AWARD_FUTURES.values(), *polymarket_us.AWARD_FUTURES.values()}
    leaders = {*kalshi.LEADER_FUTURES.values(), *(k for e in kalshi.EVENT_LEADER_FUTURES.values() for k in e.values()),
               *polymarket_us.LEADER_FUTURES.values()} - awards
    assert awards == {kind for kind, note in notes.FUTURE_NOTES.items() if note == notes.AWARD_NOTE}
    assert leaders <= {kind for kind, note in notes.FUTURE_NOTES.items() if note == notes.LEADER_NOTE}
