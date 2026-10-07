"""
Tests for the event probe's report: setting each play that settled a contract against the contract's books.
"""

import json
import pytest
from tools.event_probe import report
from tools.event_probe.store import Store

CHEAP, DEAR = [[0.4, 20.0]], [[0.99, 10.0]]


def test_the_paying_outcomes_offers_are_the_asks_for_yes_and_the_bids_turned_round_for_no():
    assert report.offers([[0.6, 10.0]], [[0.7, 5.0]], "yes") == [[0.7, 5.0]]
    assert report.offers([[0.6, 10.0]], [[0.7, 5.0]], "no") == [[0.4, 10.0]]
    assert report.cheap([[0.9, 3.0], [0.98, 4.0]], 0.97) == [[0.9, 3.0]]


def test_only_levels_that_make_money_after_fees_count():
    contracts, dollars = report.worth([[0.9, 10.0], [1.0, 5.0]], "kalshi", None)
    assert contracts == 10.0 and dollars == pytest.approx(10 * (1 - 0.9 - 0.07 * 0.9 * 0.1))


def test_the_books_say_whether_the_venue_moved_before_or_after_the_read():
    # Cheap until two seconds after the read: the feed was first, and an order then found the old price.
    books = [(0.0, [], CHEAP), (5.0, [], CHEAP), (12.0, [], DEAR)]
    assert report.follow(books, "yes", 10.0, 10.012, 0.97) == (CHEAP, CHEAP, 12.0)
    # Gone two seconds before the read: the venue was first.
    assert report.follow([(0.0, [], CHEAP), (8.0, [], DEAR)], "yes", 10.0, 10.012, 0.97) == (CHEAP, [], 8.0)
    # Never cheap: priced in before the play.
    assert report.follow([(0.0, [], DEAR), (8.0, [], CHEAP)], "yes", 10.0, 10.012, 0.97) == ([], CHEAP, None)
    # Still cheap when the books end.
    assert report.follow([(0.0, [], CHEAP)], "yes", 10.0, 10.012, 0.97) == (CHEAP, CHEAP, None)


def test_the_report_counts_each_venues_plays_by_who_was_first_and_what_was_left(tmp_path):
    store = Store(tmp_path / "probe.sqlite")
    store.add("games", ("mlb", "1", "2026-10-07", "LAD", "ATL", 0.0, 0.0))
    for venue, contract_id in (("kalshi", "K-HITS"), ("polymarket_us", "pm-hits")):
        store.add("watched", (venue, contract_id, "mlb", "1", "player_hits", "freddie freeman", 1.5, "yes", json.dumps(None)))
    # Kalshi still sold the hit at 0.40 when the read showed it, until three seconds later.
    store.add("events", (None, "mlb", "1", "kalshi", "K-HITS", "crossed", "yes", 2, 1000.0, 990.0, 995.0, "Freeman singles"))
    for ts, asks in ((950.0, CHEAP), (1003.0, DEAR)):
        store.add("books", ("kalshi", "K-HITS", ts, None, "[]", json.dumps(asks)))
    # Polymarket US's buyers of No had gone a second before the read.
    store.add("events", (None, "mlb", "1", "polymarket_us", "pm-hits", "crossed", "no", 2, 1000.0, 990.0, 995.0, "Freeman singles"))
    for ts, bids in ((950.0, [[0.7, 10.0]]), (999.0, [[0.02, 5.0]])):
        store.add("books", ("polymarket_us", "pm-hits", ts, None, json.dumps(bids), "[]"))
    store.add("events", (None, "mlb", "1", "kalshi", "K-HITS", "reversed", None, 1, 1010.0, 1005.0, 1007.0, "ruled an error"))
    for ts, made, play_ended, changed in ((996.0, 995.0, 990.0, 1), (1000.0, 995.0, 990.0, 0), (1004.0, 1003.0, 1001.0, 1)):
        store.add("reads", ("mlb", "1", ts, 0.05, 2.0, made, play_ended, changed, "live", 1, 0, "play"))
    store.flush()
    lines = report.report(store.conn)
    kalshi = next(line for line in lines if line.startswith("  mlb crossed K:"))
    assert "1 still cheap at our read (for 3.0s" in kalshi
    assert f"1 books still cheap with 20 contracts, {20 * (1 - 0.4 - 0.07 * 0.4 * 0.6):.2f}$ after fees" in kalshi
    polymarket = next(line for line in lines if line.startswith("  mlb crossed PMUS:"))
    assert "1 moved before our read (by 1.0s" in polymarket and "0 books still cheap with 0 contracts, 0.00$" in polymarket
    # MLB times the pitch: the venue moved 13 s after it and our read came 10 s after it.
    assert any("the venue moved 13.0s" in line and "our read came 10.0s" in line for line in lines)
    assert any(line.startswith("  mlb: 3 reads of 1 games") and "content changed every 8.0s" in line for line in lines)
    assert any("a pitch's end to the first read showing it 3.0s" in line for line in lines)
    assert "Settled bets undone by a later read: 1" in lines
    assert any("K-HITS pays yes: 20 contracts" in line and "cheap for 3.0s" in line for line in lines)
