"""
The venues this project knows, and the short names the reports use for them.

Whatever differs between venues is kept in a table keyed by venue, next to
the code that uses it: fees.FEES and fees.RATES, fetch.FETCHERS and fetch.SPORTS,
classify.CLASSIFIERS and classify.REPORT_GROUPS, streams.STREAMS,
settle.RESULTS and settle.RESULTS_BY_EVENT, accounts.READERS, live.PLACE,
and config.LATENCY_MS. Adding a
venue means adding it here and to each of those tables, and
tests/common/test_venues.py fails until every table has it.
"""

VENUES = ("kalshi", "polymarket_us")

SHORT_NAMES = {"kalshi": "K", "polymarket_us": "PMUS"}
