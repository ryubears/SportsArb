"""
The venues this project knows, and the short names the reports use for them.

Whatever differs between venues is kept in a table keyed by venue, next to
the code that uses it: fees.FEES and fees.RATES, fetch.FETCHERS and
fetch.SPORTS, classify.CLASSIFIERS and classify.REPORT_GROUPS,
streams.STREAMS, settle.RESULTS and settle.RESULTS_BY_EVENT, the READERS of
money/live.py, the PLACE and POSITIONS of trading/live.py,
config.PAPER_ORDER_MS and config.PAPER_FEED_SECONDS, and MAINTENANCE here.
Adding a venue means adding it here and to each of those tables, and
tests/common/test_venues.py fails until every table has it.
"""

from common.timeutil import epoch, in_weekly_window

VENUES = ("kalshi", "polymarket_us")

SHORT_NAMES = {"kalshi": "K", "polymarket_us": "PMUS"}

# Each venue's weekly maintenance, when it does not trade though its feed may go on sending books, so prices show that no order
# can trade at: as (weekday, Monday 0, start hour, end hour) in US Eastern time. Kalshi's is every Thursday from 3 to 5 AM, as
# its exchange schedule, GET /exchange/schedule, gives it, and Polymarket US's every Thursday from 6 to 8 AM, as its trading
# hours page gives it, since it publishes no status to read. A pause at any other time is met by the brakes instead: the venue
# refuses the orders, and refusals in a row halt live trading, see trading/brakes.py.
MAINTENANCE = {"kalshi": (3, 3, 5), "polymarket_us": (3, 6, 8)}


def is_maintenance(venue, now):
    """
    Whether a venue is in its weekly maintenance at now, ISO 8601 UTC, and so does not trade.
    """
    window = MAINTENANCE.get(venue)
    return window is not None and in_weekly_window(epoch(now), window)
