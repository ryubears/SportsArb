"""
Tests for the arbitrage scanner's pricing and episode detection.
"""

import pytest
import scan
from db.models import Quote

NO_FEES = {"polymarket": {"feesEnabled": False}, "kalshi": {"fee_type": "quadratic", "fee_multiplier": 0}}
REAL_FEES = {"polymarket": {"feesEnabled": True, "feeSchedule": {"rate": 0.05}},
             "kalshi": {"fee_type": "quadratic", "fee_multiplier": 1}}


def pair(pm_polarity="yes", k_polarity="yes"):
    return {"polymarket_id": "pm", "kalshi_id": "k", "kind": "spread", "season": 2027, "game_date": "2026-09-20",
            "team_a": "CAR", "team_b": "ATL", "subject": "ATL", "line": 4.5,
            "polymarket_polarity": pm_polarity, "kalshi_polarity": k_polarity}


def quote(venue, ts, bids, asks):
    return Quote(venue, "pm" if venue == "polymarket" else "k", ts, bids, asks)


# PRICING

def test_ladder_restates_bids_as_the_cost_of_the_other_side():
    q = quote("kalshi", "t", bids=[[0.53, 100]], asks=[[0.54, 50]])
    assert scan.ladder(q, "ask") == [(0.54, 50)]
    assert scan.ladder(q, "bid") == [(0.47, 100)]


def test_fill_walks_both_ladders_while_the_edge_is_positive():
    # Both legs are priced as Polymarket with fees off, so the arithmetic is exact.
    leg_a = [(0.40, 10), (0.41, 10)]
    leg_b = [(0.50, 5), (0.58, 100)]
    top_edge, size, profit = scan.fill(leg_a, leg_b, "polymarket", "polymarket", NO_FEES)
    assert top_edge == pytest.approx(0.10)
    assert size == 20
    assert profit == pytest.approx(5 * 0.10 + 5 * 0.02 + 10 * 0.01)


def test_fill_stops_at_zero_edge_and_handles_empty_ladders():
    assert scan.fill([(0.5, 10)], [(0.5, 10)], "polymarket", "polymarket", NO_FEES) == (0.0, 0.0, 0.0)
    assert scan.fill([], [(0.5, 10)], "polymarket", "polymarket", NO_FEES) == (-1.0, 0.0, 0.0)


def test_best_trade_same_polarity_buys_yes_on_the_cheap_venue():
    quotes = {"polymarket": quote("polymarket", "t", [[0.48, 100]], [[0.49, 100]]),
              "kalshi": quote("kalshi", "t", [[0.53, 100]], [[0.54, 100]])}
    name, edge, size, profit = scan.best_trade(pair(), quotes, NO_FEES)
    assert name == "buy PM yes, buy K no"
    assert edge == pytest.approx(0.04)
    assert size == 100


def test_best_trade_subtracts_fees():
    quotes = {"polymarket": quote("polymarket", "t", [[0.48, 100]], [[0.49, 100]]),
              "kalshi": quote("kalshi", "t", [[0.53, 100]], [[0.54, 100]])}
    _, gross, _, _ = scan.best_trade(pair(), quotes, NO_FEES)
    _, net, _, _ = scan.best_trade(pair(), quotes, REAL_FEES)
    assert 0 < net < gross


def test_best_trade_opposite_polarity_buys_both_sides():
    quotes = {"polymarket": quote("polymarket", "t", [[0.44, 100]], [[0.45, 100]]),
              "kalshi": quote("kalshi", "t", [[0.53, 100]], [[0.54, 100]])}
    name, edge, _, _ = scan.best_trade(pair("no", "yes"), quotes, NO_FEES)
    assert name == "buy both yes"
    assert edge == pytest.approx(1 - 0.45 - 0.54)


# EPISODES

T0 = "2026-09-19T12:00:00+00:00"


def test_scan_pair_finds_one_episode_with_duration_and_return():
    pm = [quote("polymarket", T0, [[0.48, 100]], [[0.49, 100]])]
    k = [quote("kalshi", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]]),
         quote("kalshi", "2026-09-19T12:01:01+00:00", [[0.49, 100]], [[0.50, 100]])]
    close = "2026-09-29T12:00:01+00:00"
    episodes = scan.scan_pair(pair(), pm, k, NO_FEES, None, close)
    assert len(episodes) == 1
    o = episodes[0]
    assert (o.start_ts, o.end_ts, o.seconds) == ("2026-09-19T12:00:01+00:00", "2026-09-19T12:01:01+00:00", 60)
    assert o.peak_edge == pytest.approx(0.04)
    assert o.peak_size == 100
    assert o.live == 0
    assert o.days_held == pytest.approx(10)
    assert o.return_pct == pytest.approx(100 * 0.04 / 0.96)
    assert o.annual_pct == pytest.approx(o.return_pct * 365 / 10)


def test_scan_pair_marks_live_and_uses_kickoff_for_payout():
    pm = [quote("polymarket", T0, [[0.48, 100]], [[0.49, 100]])]
    k = [quote("kalshi", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]])]
    kickoff = "2026-09-19T11:00:00+00:00"
    o = scan.scan_pair(pair(), pm, k, NO_FEES, kickoff, "2026-09-19T11:00:00+00:00")[0]
    assert o.live == 1
    assert o.end_ts == "2026-09-19T12:00:01+00:00"
    assert o.days_held == pytest.approx((scan.GAME_HOURS - 1) / 24, rel=1e-3)


def test_scan_pair_ignores_time_before_both_books_exist():
    pm = [quote("polymarket", T0, [[0.48, 100]], [[0.49, 100]])]
    assert scan.scan_pair(pair(), pm, [], NO_FEES, None, None) == []


def test_label():
    assert scan.label(pair()) == "spread 2026-09-20 CAR@ATL ATL 4.5"
