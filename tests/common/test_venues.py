"""
Every table of venue specific code covers every venue, and nothing else.
"""

import pytest
from catalog import fetch
from catalog.classify import classify
from common.venues import MAINTENANCE, SHORT_NAMES, VENUES, is_maintenance
from engine.components.market import streams
from engine.components.money import live as money_live
from engine.components.money import settle
from engine.components.trading import live as trading_live
from engine.helper import config, fees


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
    ("money/live.READERS", money_live.READERS),
    ("trading/live.PLACE", trading_live.PLACE),
    ("trading/live.POSITIONS", trading_live.POSITIONS),
    ("MAINTENANCE", MAINTENANCE),
    ("config.PAPER_ORDER_MS", config.PAPER_ORDER_MS),
    ("config.PAPER_FEED_SECONDS", config.PAPER_FEED_SECONDS),
])
def test_table_covers_every_venue(name, table):
    assert set(table) == set(VENUES), name


def test_each_venue_is_in_maintenance_on_thursdays_in_eastern_time():
    # Thursday, October 1, 2026, Eastern daylight time: Kalshi from 3 to 5 AM, 07:00 to 09:00 UTC, Polymarket US from 6 to
    # 8 AM, 10:00 to 12:00 UTC.
    m = is_maintenance
    assert not m("kalshi", "2026-10-01T06:59:00+00:00") and m("kalshi", "2026-10-01T07:00:00+00:00")
    assert m("kalshi", "2026-10-01T08:59:00+00:00") and not m("kalshi", "2026-10-01T09:00:00+00:00")
    assert not m("polymarket_us", "2026-10-01T08:00:00+00:00") and m("polymarket_us", "2026-10-01T10:00:00+00:00")
    assert m("polymarket_us", "2026-10-01T11:59:00+00:00") and not m("polymarket_us", "2026-10-01T12:00:00+00:00")
    assert m("kalshi", "2026-12-03T08:30:00+00:00")                         # 3:30 AM Eastern standard time, an hour later in UTC.
    assert not m("kalshi", "2026-09-30T07:30:00+00:00")                     # A Wednesday.
    assert not m("gemini", "2026-10-01T07:30:00+00:00")                     # A venue with no window never is.

