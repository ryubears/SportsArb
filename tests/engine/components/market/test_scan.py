"""
Tests for the scanner's episode detection over the recorder's in memory books.
"""

import asyncio
import pytest
from common.timeutil import days_between
from db import database
from db.models import Bet, Pair, Contract, Opportunity, Book
from engine.components.market import record, scan
from engine.helper import game

NO_PM_FEES = {"feeCoefficient": 0}
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}
T0 = "2026-09-19T12:00:00+00:00"
LABEL = "spread 2026-09-20 CAR@ATL ATL 4.5"


def member(venue, contract_id, polarity="yes", start_time=None, close_time="2026-09-29T12:00:00+00:00"):
    fee_info = NO_K_FEES if venue == "kalshi" else NO_PM_FEES
    return dict(venue=venue, contract_id=contract_id, polarity=polarity, start_time=start_time, close_time=close_time, fee_info=fee_info)


def make_db(tmp_path, members, future=False):
    """
    A database holding one spread pair with these members, so a Scanner can
    load it, or with future, one Super Bowl winner pair.
    """
    conn = database.connect(tmp_path / "t.sqlite")
    contracts = [Contract(venue=m["venue"], contract_id=m["contract_id"], market_id=m["contract_id"], event_id="e", series_id=None,
                          sport="nfl", event_title=None, title="t", outcome="Yes", market_type=None, line=None, rules=None,
                          start_time=m["start_time"], close_time=m["close_time"], fee_info=m["fee_info"]) for m in members]
    database.upsert_contracts(conn, contracts, "2026-09-19T00:00:00+00:00")
    if future:
        bets = [Bet(m["venue"], m["contract_id"], "champion", 2027, None, None, None, "ATL", None, m["polarity"]) for m in members]
        pair = Pair("nfl champion 2027 ATL", "champion", 2027, None, None, None, "ATL", None, bets, [], sport="nfl")
    else:
        bets = [Bet(m["venue"], m["contract_id"], "spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, m["polarity"]) for m in members]
        pair = Pair(LABEL, "spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, bets, [], sport="nfl")
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", [pair], "2026-09-19T00:00:00+00:00")
    return conn


def stored(conn):
    """
    The Opportunities in the database, oldest first.
    """
    return [Opportunity(*row) for row in conn.execute("""
        SELECT pair_id, trade, yes_venue, yes_contract, no_venue, no_contract, start_ts, end_ts, seconds, peak_ts,
               peak_edge, peak_size, peak_profit, live, days_held, return_pct, annual_pct FROM opportunities ORDER BY start_ts""")]


def stretches(conn):
    """
    Each stored episode's longest stretch at the minimum edge, as (seconds, contracts, profit), oldest first.
    """
    return [tuple(r) for r in conn.execute("SELECT min_edge_seconds, min_edge_size, min_edge_profit FROM opportunities ORDER BY start_ts")]


def replay(conn, books, drops=()):
    """
    Drive a Scanner with books in time order, the way the recorder drives it live, and return what it stored.
    drops are (ts, venue) pairs at which the venue's books are forgotten.
    """
    scanner = scan.Scanner(conn, ("nfl",), lambda m: None)
    events = sorted(books, key=lambda b: b.ts)
    held = {}           # The newest book of each contract, as the recorder holds them.
    for b in events:
        for ts, venue in drops:
            if ts <= b.ts and not any(x.ts >= ts for k, x in held.items() if k[0] == venue):
                held = {k: x for k, x in held.items() if k[0] != venue}
        held[(b.venue, b.contract_id)] = b
        scanner.on_book(b.venue, b.contract_id, held, b.ts)
        scanner.tick(held, b.ts)
    if events:
        scanner.tick({}, events[-1].ts)
    return stored(conn)


# EPISODES

def test_scanner_finds_one_episode_with_duration_and_return(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    episodes = replay(conn, [Book("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                             Book("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]]),
                             Book("kalshi", "k", "2026-09-19T12:01:01+00:00", [[0.49, 100]], [[0.50, 100]])])
    assert len(episodes) == 1
    o = episodes[0]
    assert (o.start_ts, o.end_ts, o.seconds) == ("2026-09-19T12:00:01+00:00", "2026-09-19T12:01:01+00:00", 60)
    assert (o.yes_venue, o.no_venue) == ("polymarket_us", "kalshi")
    assert o.trade == "yes: PMUS buy, no: K buy other side"
    assert o.peak_edge == pytest.approx(0.04)
    assert o.peak_size == 100
    assert o.live == 0
    assert o.days_held == pytest.approx(10, rel=1e-3)
    assert o.return_pct == pytest.approx(100 * 0.04 / 0.96)
    assert o.annual_pct == pytest.approx(o.return_pct * 365 / 10, rel=1e-3)


def test_an_episode_keeps_its_longest_stretch_at_the_minimum_edge_and_what_stayed_fillable_through_it(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    at = "2026-09-19T12:00:%02d+00:00"
    episodes = replay(conn, [Book("polymarket_us", "pm", at % 0, [[0.39, 100]], [[0.40, 100]]),     # Yes costs 0.40 throughout.
                             Book("kalshi", "k", at % 1, [[0.53, 100]], [[0.99, 1]]),       # No costs 0.47: 13 cents on 100.
                             Book("kalshi", "k", at % 2, [[0.53, 40], [0.41, 60]], [[0.99, 1]]),    # 13 cents on only 40.
                             Book("kalshi", "k", at % 4, [[0.41, 100]], [[0.99, 1]]),       # 1 cent: the stretch ends after 3s.
                             Book("kalshi", "k", at % 5, [[0.50, 100]], [[0.99, 1]]),       # 10 cents again, for 1s.
                             Book("kalshi", "k", at % 6, [[0.40, 100]], [[0.99, 1]])])      # No edge: the episode ends.
    assert [(o.start_ts, o.end_ts) for o in episodes] == [(at % 1, at % 6)]
    assert stretches(conn) == [(3.0, 40.0, pytest.approx(40 * 0.13))]


def test_a_moment_at_the_minimum_edge_keeps_its_size(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    at = "2026-09-19T12:00:%02d+00:00"
    replay(conn, [Book("polymarket_us", "pm", at % 0, [[0.39, 100]], [[0.40, 100]]),
                  Book("kalshi", "k", at % 1, [[0.53, 100]], [[0.99, 1]]),       # 13 cents on 100, for a moment.
                  Book("kalshi", "k", at % 1, [[0.41, 100]], [[0.99, 1]]),       # Then 1 cent, in the same moment.
                  Book("kalshi", "k", at % 3, [[0.40, 100]], [[0.99, 1]])])
    assert stretches(conn) == [(0.0, 100.0, pytest.approx(13.0))]


def test_an_episode_never_at_the_minimum_edge_has_no_stretch(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    replay(conn, [Book("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                  Book("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.50, 100]], [[0.51, 100]]),     # 1 cent.
                  Book("kalshi", "k", "2026-09-19T12:01:01+00:00", [[0.49, 100]], [[0.50, 100]])])
    assert stretches(conn) == [(0.0, 0.0, 0.0)]


def test_a_games_book_ages_only_once_the_game_may_have_started(tmp_path):
    books = {("polymarket_us", "pm"): Book("polymarket_us", "pm", T0, [[0.39, 100]], [[0.40, 100]]),
             ("kalshi", "k"): Book("kalshi", "k", T0, [[0.53, 100]], [[0.99, 1]])}
    for kickoff, still_open in (("2026-09-20T17:00:00+00:00", True), ("2026-09-19T12:30:00+00:00", False)):
        conn = make_db(tmp_path / kickoff[:13], [member("kalshi", "k"), member("polymarket_us", "pm", start_time=kickoff)])
        scanner = scan.Scanner(conn, ("nfl",), lambda m: None)
        scanner.on_book("kalshi", "k", books, T0)
        scanner.tick(books, "2026-09-19T13:00:00+00:00")     # An hour on, the books unchanged: a day before kickoff, or after it.
        assert bool(scanner.episodes) is still_open, kickoff


def test_a_futures_book_does_not_age_while_a_games_does(tmp_path):
    books = {("polymarket_us", "pm"): Book("polymarket_us", "pm", T0, [[0.39, 100]], [[0.40, 100]]),
             ("kalshi", "k"): Book("kalshi", "k", T0, [[0.53, 100]], [[0.99, 1]])}
    hour_later = "2026-09-19T13:00:00+00:00"
    for future, still_open in ((True, True), (False, False)):
        conn = make_db(tmp_path / str(future), [member("kalshi", "k"), member("polymarket_us", "pm")], future=future)
        scanner = scan.Scanner(conn, ("nfl",), lambda m: None)
        scanner.on_book("kalshi", "k", books, T0)
        scanner.tick(books, hour_later)         # Neither book changed in the hour.
        assert bool(scanner.episodes) is still_open, future


def test_scanner_marks_live_and_uses_kickoff_for_payout(tmp_path):
    kickoff = "2026-09-19T11:00:00+00:00"
    conn = make_db(tmp_path, [member("kalshi", "k", start_time=kickoff), member("polymarket_us", "pm", start_time=kickoff)])
    o = replay(conn, [Book("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                      Book("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]])])[0]
    assert o.live == 1
    assert o.end_ts == "2026-09-19T12:00:01+00:00"
    assert o.days_held == pytest.approx(days_between(o.peak_ts, game.money_back(game.expected_end(kickoff, "nfl"))))


def test_scanner_holds_until_the_slower_leg_pays(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k", close_time="2026-10-19T12:00:00+00:00"),
                              member("polymarket_us", "pm", close_time="2026-09-29T12:00:00+00:00")])
    o = replay(conn, [Book("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                      Book("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]])])[0]
    assert o.days_held == pytest.approx(30, rel=1e-4)


def test_scanner_ignores_a_member_whose_book_went_stale(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    # Polymarket US quoted once, then went quiet. Two minutes later Kalshi reprices and would appear to cross it.
    assert replay(conn, [Book("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                         Book("kalshi", "k", "2026-09-19T12:02:01+00:00", [[0.53, 100]], [[0.54, 100]])]) == []


def test_scanner_ignores_a_book_from_before_its_venue_dropped(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    # Polymarket US quoted at 12:00:00, dropped at 12:00:30, and requoted the same book at 12:01:30. Kalshi crosses it at 12:01:00.
    books = [Book("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
              Book("polymarket_us", "pm", "2026-09-19T12:01:30+00:00", [[0.48, 100]], [[0.49, 100]]),
              Book("kalshi", "k", "2026-09-19T12:01:00+00:00", [[0.53, 100]], [[0.54, 100]])]
    assert len(replay(conn, books)) == 1
    conn.execute("DELETE FROM opportunities")
    episodes = replay(conn, books, drops=[("2026-09-19T12:00:30+00:00", "polymarket_us")])
    assert [o.start_ts for o in episodes] == ["2026-09-19T12:01:30+00:00"]     # Only once Polymarket is seen again.


def test_scanner_ignores_time_before_two_members_have_books(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    assert replay(conn, [Book("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]])]) == []


# LIVE

TL = "2026-09-20T17:%02d:%02d+00:00"
KICKOFF = "2026-09-20T17:00:00+00:00"


def book(venue, cid, ts, bid, ask, size=100):
    return Book(venue, cid, ts, [[bid, size]], [[ask, size]])


def game_db(tmp_path):
    return make_db(tmp_path, [member("kalshi", "k", start_time=KICKOFF, close_time="2026-09-20T21:00:00+00:00"),
                              member("polymarket_us", "pm", start_time=KICKOFF, close_time="2026-09-20T21:00:00+00:00")])


def test_episode_opens_peaks_and_closes_from_book_changes(tmp_path):
    conn = game_db(tmp_path)
    logs = []
    s = scan.Scanner(conn, ("nfl",), logs.append)
    latest = {("kalshi", "k"): book("kalshi", "k", TL % (0, 1), 0.53, 0.54)}
    s.on_book("kalshi", "k", latest, TL % (0, 1))
    assert s.episodes == {}                                     # One book is not a trade.
    latest[("polymarket_us", "pm")] = book("polymarket_us", "pm", TL % (0, 2), 0.44, 0.45)
    s.on_book("polymarket_us", "pm", latest, TL % (0, 2))          # Buy yes at 0.45, no at 1 - 0.53: 2c edge.
    assert [s.pairs[i]["label"] for i in s.episodes] == [LABEL]
    latest[("polymarket_us", "pm")] = book("polymarket_us", "pm", TL % (0, 3), 0.40, 0.41)
    s.on_book("polymarket_us", "pm", latest, TL % (0, 3))          # 12c edge, a new peak.
    latest[("kalshi", "k")] = book("kalshi", "k", TL % (0, 5), 0.40, 0.41)
    s.on_book("kalshi", "k", latest, TL % (0, 5))               # Kalshi catches up, edge gone.
    assert s.episodes == {}
    o = stored(conn)[0]
    assert (o.start_ts, o.end_ts, o.peak_ts, round(o.peak_edge, 2), o.peak_size, o.live) == (TL % (0, 2), TL % (0, 5), TL % (0, 3), 0.12, 100, 1)
    assert logs == [f"episode {LABEL}: yes: PMUS buy, no: K buy other side, 12.0c x 100 = 12.00$, lasted 3.0s, "
                    "2c or more for 3.0s with 100 contracts throughout"]
    assert s.summary().startswith("scanner: spread 1 episodes, 1 beat target, best 12.00$ for 3s; 0 open")
    assert s.summary() == "scanner: no episodes; 0 open"


def test_sweep_closes_an_episode_whose_book_went_stale_or_unseen(tmp_path):
    conn = game_db(tmp_path)
    s = scan.Scanner(conn, ("nfl",), lambda m: None)
    latest = {("kalshi", "k"): book("kalshi", "k", TL % (0, 1), 0.53, 0.54),
              ("polymarket_us", "pm"): book("polymarket_us", "pm", TL % (0, 2), 0.44, 0.45)}
    s.on_book("polymarket_us", "pm", latest, TL % (0, 2))
    s.tick(latest, TL % (0, 30))
    assert len(s.episodes) == 1                                 # Still fresh.
    s.tick(latest, TL % (1, 30))                               # Kalshi's book is now 89 seconds old.
    assert s.episodes == {}
    assert stored(conn)[0].end_ts == TL % (1, 30)
    latest = {("kalshi", "k"): book("kalshi", "k", TL % (1, 31), 0.53, 0.54),
              ("polymarket_us", "pm"): book("polymarket_us", "pm", TL % (1, 31), 0.44, 0.45)}
    s.on_book("kalshi", "k", latest, TL % (1, 31))
    assert len(s.episodes) == 1
    del latest[("polymarket_us", "pm")]                            # The recorder forgot Polymarket US's books after a drop.
    s.tick(latest, TL % (1, 32))
    assert s.episodes == {}


def test_reload_ends_episodes_of_pairs_that_vanished(tmp_path):
    conn = game_db(tmp_path)
    s = scan.Scanner(conn, ("nfl",), lambda m: None)
    latest = {("kalshi", "k"): book("kalshi", "k", TL % (0, 1), 0.53, 0.54),
              ("polymarket_us", "pm"): book("polymarket_us", "pm", TL % (0, 2), 0.44, 0.45)}
    s.on_book("polymarket_us", "pm", latest, TL % (0, 2))
    database.replace_pairs(conn, "nfl", [], "2026-09-20T18:00:00+00:00")
    s.reload()
    assert s.pairs == {} and s.episodes == {} and s.by_contract == {}
    assert len(stored(conn)) == 1


def test_recorder_prices_only_top_of_book_changes(tmp_path):
    conn = game_db(tmp_path)
    calls = []

    class FakeScanner:
        def on_book(self, venue, cid, latest, now):
            calls.append((venue, cid))

    r = record.Recorder(conn, FakeScanner())
    r.on_book("kalshi", "k", [[0.5, 10], [0.49, 5]], [[0.52, 7]])
    r.on_book("kalshi", "k", [[0.5, 10], [0.48, 5]], [[0.52, 7]])     # Only a deeper level moved.
    r.on_book("kalshi", "k", [[0.5, 11]], [[0.52, 7]])                # Size at the top moved.
    assert calls == [("kalshi", "k"), ("kalshi", "k")]


def test_a_pair_a_desk_waits_on_is_priced_again_once_the_wait_ends(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")], future=True)
    latest = {("kalshi", "k"): book("kalshi", "k", T0, 0.53, 0.54), ("polymarket_us", "pm"): book("polymarket_us", "pm", T0, 0.40, 0.41)}
    offered = []
    s = scan.Scanner(conn, ("nfl",), lambda m: None, books=lambda: latest)

    def desk(pair, yes, no, edge, size, fee_infos, now):
        offered.append(now)
        if len(offered) == 1:                       # Waits, as for a Polymarket US book to catch up with a Kalshi move.
            s.recheck(pair["id"], 0.05)
            s.recheck(pair["id"], 0.5)              # A later ask keeps the sooner timer.
            return False
        return True

    async def scenario():
        s.on_book("polymarket_us", "pm", latest, T0)
        assert len(s.rechecks) == 1
        await asyncio.sleep(0.2)                    # No book changes, and the tick is a second away.

    s.on_signals.append(desk)
    asyncio.run(scenario())
    assert len(offered) == 2 and offered[1] > T0 and s.rechecks == {}


def test_a_recheck_needs_a_running_loop_and_ends_with_its_episode(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")], future=True)
    latest = {("kalshi", "k"): book("kalshi", "k", T0, 0.53, 0.54), ("polymarket_us", "pm"): book("polymarket_us", "pm", T0, 0.40, 0.41)}
    s = scan.Scanner(conn, ("nfl",), lambda m: None, books=lambda: latest)
    s.on_book("polymarket_us", "pm", latest, T0)
    pair_id = next(iter(s.episodes))
    s.recheck(pair_id, 0.05)                        # Outside a loop the next change or tick prices it.
    assert s.rechecks == {}

    async def scenario():
        s.recheck(pair_id, 0.05)
        timer = s.rechecks[pair_id]
        latest[("kalshi", "k")] = book("kalshi", "k", T0, 0.40, 0.41)
        s.on_book("kalshi", "k", latest, T0)        # The edge is gone, so its episode ends, and the timer with it.
        return timer
    timer = asyncio.run(scenario())
    assert s.episodes == {} and s.rechecks == {} and timer.cancelled()
