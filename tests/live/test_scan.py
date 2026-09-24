"""
Tests for the scanner's episode detection over the recorder's in memory books.
"""

import pytest
from db import database
from db.models import Bet, Pair, Contract, Opportunity, Quote
from live import record, scan

NO_PM_FEES = {"feeCoefficient": 0}
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}
T0 = "2026-09-19T12:00:00+00:00"
LABEL = "spread 2026-09-20 CAR@ATL ATL 4.5"


def member(venue, contract_id, polarity="yes", start_time=None, close_time="2026-09-29T12:00:00+00:00"):
    fee_info = NO_K_FEES if venue == "kalshi" else NO_PM_FEES
    return dict(venue=venue, contract_id=contract_id, polarity=polarity, start_time=start_time, close_time=close_time, fee_info=fee_info)


def make_db(tmp_path, members):
    """
    A database holding one spread pair with these members, so a Scanner can load it.
    """
    conn = database.connect(tmp_path / "t.sqlite")
    contracts = [Contract(venue=m["venue"], contract_id=m["contract_id"], market_id=m["contract_id"], event_id="e", series_id=None,
                          sport="nfl", event_title=None, title="t", outcome="Yes", market_type=None, line=None, rules=None,
                          start_time=m["start_time"], close_time=m["close_time"], fee_info=m["fee_info"]) for m in members]
    database.upsert_contracts(conn, contracts, "2026-09-19T00:00:00+00:00")
    bets = [Bet(m["venue"], m["contract_id"], "spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, m["polarity"]) for m in members]
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", [Pair(LABEL, "spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, bets, [])],
                            "2026-09-19T00:00:00+00:00")
    return conn


def quote(venue, contract_id, ts, bids, asks):
    return Quote(venue, contract_id, ts, bids, asks)


def stored(conn):
    """
    The Opportunities in the database, oldest first.
    """
    return [Opportunity(*row) for row in conn.execute("""
        SELECT pair_id, trade, yes_venue, yes_contract, no_venue, no_contract, start_ts, end_ts, seconds, peak_ts,
               peak_edge, peak_size, peak_profit, live, days_held, return_pct, annual_pct FROM opportunities ORDER BY start_ts""")]


def replay(conn, quotes, drops=()):
    """
    Drive a Scanner with quotes in time order, the way the recorder drives it live, and return what it stored.
    drops are (ts, venue) pairs at which the venue's books are forgotten.
    """
    scanner = scan.Scanner(conn, "nfl", lambda m: None)
    events = sorted(quotes, key=lambda q: q.ts)
    latest = {}
    for q in events:
        for ts, venue in drops:
            if ts <= q.ts and not any(x.ts >= ts for k, x in latest.items() if k[0] == venue):
                latest = {k: x for k, x in latest.items() if k[0] != venue}
        latest[(q.venue, q.contract_id)] = q
        scanner.on_book(q.venue, q.contract_id, latest, q.ts)
        scanner.sweep(latest, q.ts)
    if events:
        scanner.sweep({}, events[-1].ts)
    return stored(conn)


# EPISODES

def test_scanner_finds_one_episode_with_duration_and_return(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    episodes = replay(conn, [quote("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                             quote("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]]),
                             quote("kalshi", "k", "2026-09-19T12:01:01+00:00", [[0.49, 100]], [[0.50, 100]])])
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


def test_scanner_marks_live_and_uses_kickoff_for_payout(tmp_path):
    kickoff = "2026-09-19T11:00:00+00:00"
    conn = make_db(tmp_path, [member("kalshi", "k", start_time=kickoff), member("polymarket_us", "pm", start_time=kickoff)])
    o = replay(conn, [quote("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                      quote("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]])])[0]
    assert o.live == 1
    assert o.end_ts == "2026-09-19T12:00:01+00:00"
    assert o.days_held == pytest.approx((scan.GAME_HOURS - 1) / 24, rel=1e-3)


def test_scanner_holds_until_the_slower_leg_pays(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k", close_time="2026-10-19T12:00:00+00:00"),
                              member("polymarket_us", "pm", close_time="2026-09-29T12:00:00+00:00")])
    o = replay(conn, [quote("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                      quote("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]])])[0]
    assert o.days_held == pytest.approx(30, rel=1e-4)


def test_scanner_ignores_a_member_whose_book_went_stale(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    # Polymarket US quoted once, then went quiet. Two minutes later Kalshi reprices and would appear to cross it.
    assert replay(conn, [quote("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                         quote("kalshi", "k", "2026-09-19T12:02:01+00:00", [[0.53, 100]], [[0.54, 100]])]) == []


def test_scanner_ignores_a_book_from_before_its_venue_dropped(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    # Polymarket US quoted at 12:00:00, dropped at 12:00:30, and requoted the same book at 12:01:30. Kalshi crosses it at 12:01:00.
    quotes = [quote("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
              quote("polymarket_us", "pm", "2026-09-19T12:01:30+00:00", [[0.48, 100]], [[0.49, 100]]),
              quote("kalshi", "k", "2026-09-19T12:01:00+00:00", [[0.53, 100]], [[0.54, 100]])]
    assert len(replay(conn, quotes)) == 1
    conn.execute("DELETE FROM opportunities")
    episodes = replay(conn, quotes, drops=[("2026-09-19T12:00:30+00:00", "polymarket_us")])
    assert [o.start_ts for o in episodes] == ["2026-09-19T12:01:30+00:00"]     # Only once Polymarket is seen again.


def test_scanner_ignores_time_before_two_members_have_books(tmp_path):
    conn = make_db(tmp_path, [member("kalshi", "k"), member("polymarket_us", "pm")])
    assert replay(conn, [quote("polymarket_us", "pm", T0, [[0.48, 100]], [[0.49, 100]])]) == []


# LIVE

TL = "2026-09-20T17:%02d:%02d+00:00"
KICKOFF = "2026-09-20T17:00:00+00:00"


def book(venue, cid, ts, bid, ask, size=100):
    return Quote(venue, cid, ts, [[bid, size]], [[ask, size]])


def game_db(tmp_path):
    return make_db(tmp_path, [member("kalshi", "k", start_time=KICKOFF, close_time="2026-09-20T21:00:00+00:00"),
                              member("polymarket_us", "pm", start_time=KICKOFF, close_time="2026-09-20T21:00:00+00:00")])


def test_episode_opens_peaks_and_closes_from_book_changes(tmp_path):
    conn = game_db(tmp_path)
    logs = []
    s = scan.Scanner(conn, "nfl", logs.append)
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
    assert logs == [f"episode {LABEL}: yes: PMUS buy, no: K buy other side, 12.0c x 100 = 12.00$, lasted 3.0s"]
    assert s.summary().startswith("scanner: spread 1 episodes, 1 beat target, best 12.00$ for 3s; 0 open")
    assert s.summary() == "scanner: no episodes; 0 open"


def test_sweep_closes_an_episode_whose_book_went_stale_or_unseen(tmp_path):
    conn = game_db(tmp_path)
    s = scan.Scanner(conn, "nfl", lambda m: None)
    latest = {("kalshi", "k"): book("kalshi", "k", TL % (0, 1), 0.53, 0.54),
              ("polymarket_us", "pm"): book("polymarket_us", "pm", TL % (0, 2), 0.44, 0.45)}
    s.on_book("polymarket_us", "pm", latest, TL % (0, 2))
    s.sweep(latest, TL % (0, 30))
    assert len(s.episodes) == 1                                 # Still fresh.
    s.sweep(latest, TL % (1, 30))                               # Kalshi's book is now 89 seconds old.
    assert s.episodes == {}
    assert stored(conn)[0].end_ts == TL % (1, 30)
    latest = {("kalshi", "k"): book("kalshi", "k", TL % (1, 31), 0.53, 0.54),
              ("polymarket_us", "pm"): book("polymarket_us", "pm", TL % (1, 31), 0.44, 0.45)}
    s.on_book("kalshi", "k", latest, TL % (1, 31))
    assert len(s.episodes) == 1
    del latest[("polymarket_us", "pm")]                            # The recorder forgot Polymarket US's books after a drop.
    s.sweep(latest, TL % (1, 32))
    assert s.episodes == {}


def test_reload_ends_episodes_of_pairs_that_vanished(tmp_path):
    conn = game_db(tmp_path)
    s = scan.Scanner(conn, "nfl", lambda m: None)
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
