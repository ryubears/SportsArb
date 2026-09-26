"""
Every table of venue specific code covers every venue, and nothing else.
"""

import pytest
from catalog import fetch
from catalog.classify import classify
from common.venues import SHORT_NAMES, VENUES
from live.components import settle, streams
from live.helper import config, fees


@pytest.mark.parametrize("name, table", [
    ("SHORT_NAMES", SHORT_NAMES),
    ("fees.FEES", fees.FEES),
    ("fees.RATES", fees.RATES),
    ("fetch.FETCHERS", fetch.FETCHERS),
    *((f"fetch.SPORTS[{sport!r}]", config) for sport, config in fetch.SPORTS.items()),
    ("classify.CLASSIFIERS", classify.CLASSIFIERS),
    ("classify.REPORT_GROUPS", classify.REPORT_GROUPS),
    ("streams.STREAMS", streams.STREAMS),
    ("settle.RESULTS", settle.RESULTS),
    ("settle.RESULTS_BY_EVENT", settle.RESULTS_BY_EVENT),
    ("config.LATENCY_MS", config.LATENCY_MS),
])
def test_table_covers_every_venue(name, table):
    assert set(table) == set(VENUES), name
