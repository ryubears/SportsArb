"""
Tests for pricing one side of a bet through a contract and both sides across a group.
"""

import pytest
from live.price import pricing
from db.models import Quote

PM_FEES = {"feeCoefficient": 0.0695}
NO_PM_FEES = {"feeCoefficient": 0}
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}
REAL_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 1}


def member(venue, contract_id, polarity="yes"):
    return {"venue": venue, "contract_id": contract_id, "polarity": polarity}


def quote(venue, contract_id, ts, bids, asks):
    return Quote(venue, contract_id, ts, bids, asks)


def test_ladder_depends_on_which_side_the_contract_pays():
    q = quote("kalshi", "k", "t", bids=[[0.53, 100]], asks=[[0.54, 50]])
    assert pricing.ladder(q, "yes", "yes") == [(0.54, 50)]     # Hold yes through a yes contract, buy it.
    assert pricing.ladder(q, "yes", "no") == [(0.47, 100)]     # Hold no through a yes contract, buy the other side.
    assert pricing.ladder(q, "no", "no") == [(0.54, 50)]       # Hold no through a no contract, buy it.
    assert pricing.ladder(q, "no", "yes") == [(0.47, 100)]


def test_depth_stops_where_the_edge_falls_under_the_floor():
    fee = ("polymarket_us", {"feeCoefficient": 0})
    leg_a = [(0.45, 1), (0.46, 100), (0.50, 100)]     # Hold yes: 8, 7, then 3 cents against 0.47 on the other leg.
    leg_b = [(0.47, 100)]
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.05) == (0.46, 0.47, 100)      # The third level is under 5 cents.
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.08) == (0.45, 0.47, 1)        # Only the top level clears 8 cents.
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.09) == (None, None, 0)        # Nothing does.


def test_positive_depth_walks_both_ladders_while_the_edge_is_positive():
    leg_a = [(0.40, 10), (0.41, 10)]
    leg_b = [(0.50, 5), (0.58, 100)]
    fee = ("polymarket_us", NO_PM_FEES)
    top_edge, size, profit = pricing.positive_depth(leg_a, leg_b, fee, fee)
    assert top_edge == pytest.approx(0.10)
    assert size == 20
    assert profit == pytest.approx(5 * 0.10 + 5 * 0.02 + 10 * 0.01)


def test_positive_depth_stops_at_zero_edge_and_handles_empty_ladders():
    fee = ("polymarket_us", NO_PM_FEES)
    assert pricing.positive_depth([(0.5, 10)], [(0.5, 10)], fee, fee) == (0.0, 0.0, 0.0)
    assert pricing.positive_depth([], [(0.5, 10)], fee, fee) == (-1.0, 0.0, 0.0)


def test_positive_depth_leaves_the_ladders_as_they_were():
    leg_a, leg_b = [(0.40, 10)], [(0.50, 5)]
    fee = ("polymarket_us", NO_PM_FEES)
    pricing.positive_depth(leg_a, leg_b, fee, fee)
    assert (leg_a, leg_b) == ([(0.40, 10)], [(0.50, 5)])


def test_sweep_takes_a_share_of_each_level_skips_levels_too_small_and_stops_at_the_limit():
    levels = [(0.40, 1), (0.41, 10), (0.45, 100), (0.60, 100)]
    fee = ("polymarket_us", NO_PM_FEES)
    # Half of 1 is no whole contract, so the top level is skipped. Then 5 at 0.41 and the rest at 0.45, and 0.60 is over the limit.
    assert pricing.sweep(levels, 30, *fee, share=0.5, limit=0.50) == (30, pytest.approx(5 * 0.41 + 25 * 0.45))
    assert pricing.sweep(levels, 500, *fee, share=0.5, limit=0.50) == (55, pytest.approx(5 * 0.41 + 50 * 0.45))
    # Selling takes the fee off the proceeds instead of adding it.
    pm = ("polymarket_us", {"feeCoefficient": 0.0695})
    bought, sold = pricing.sweep([(0.5, 10)], 10, *pm), pricing.sweep([(0.5, 10)], 10, *pm, selling=True)
    assert bought[1] - 5.0 == pytest.approx(5.0 - sold[1]) and bought[1] > 5.0


def test_best_trade_picks_the_cheapest_leg_on_each_side_across_venues():
    members = [member("kalshi", "k"), member("polymarket_us", "us")]
    quotes = {("kalshi", "k"): quote("kalshi", "k", "t", [[0.53, 100]], [[0.54, 100]]),
              ("polymarket_us", "us"): quote("polymarket_us", "us", "t", [[0.44, 100]], [[0.45, 100]])}
    fee_infos = {("kalshi", "k"): NO_K_FEES, ("polymarket_us", "us"): NO_PM_FEES}
    yes, no, edge, size, profit = pricing.best_trade(members, quotes, fee_infos)
    # Yes is cheapest at Polymarket's 0.45 ask. No is cheapest at Kalshi, one minus its 0.53 bid.
    assert (yes["venue"], no["venue"]) == ("polymarket_us", "kalshi")
    assert edge == pytest.approx(1 - 0.45 - 0.47)
    assert size == 100


def test_best_trade_uses_a_no_contract_for_yes_exposure():
    members = [member("kalshi", "k_yes", "yes"), member("kalshi", "k_no", "no")]
    quotes = {("kalshi", "k_yes"): quote("kalshi", "k_yes", "t", [[0.40, 100]], [[0.60, 100]]),
              ("kalshi", "k_no"): quote("kalshi", "k_no", "t", [[0.55, 100]], [[0.70, 100]])}
    fee_infos = {("kalshi", "k_yes"): NO_K_FEES, ("kalshi", "k_no"): NO_K_FEES}
    yes, no, edge, _, _ = pricing.best_trade(members, quotes, fee_infos)
    # Yes through the no contract's bid costs 0.45, cheaper than the yes contract's 0.60 ask.
    # No through the yes contract's bid costs 0.60, cheaper than the no contract's 0.70 ask.
    assert (yes["contract_id"], no["contract_id"]) == ("k_no", "k_yes")
    assert edge == pytest.approx(1 - 0.45 - 0.60)


def test_best_trade_never_uses_one_contract_for_both_legs():
    # k_no is cheapest on both sides, so it is paired with the other contract on whichever side works out better.
    members = [member("kalshi", "k_yes", "yes"), member("kalshi", "k_no", "no")]
    quotes = {("kalshi", "k_yes"): quote("kalshi", "k_yes", "t", [[0.40, 100]], [[0.60, 100]]),
              ("kalshi", "k_no"): quote("kalshi", "k_no", "t", [[0.55, 100]], [[0.56, 100]])}
    fee_infos = {("kalshi", "k_yes"): NO_K_FEES, ("kalshi", "k_no"): NO_K_FEES}
    yes, no, edge, _, _ = pricing.best_trade(members, quotes, fee_infos)
    assert (yes["contract_id"], no["contract_id"]) == ("k_no", "k_yes")
    assert edge == pytest.approx(1 - 0.45 - 0.60)


def test_best_trade_ignores_a_crossed_book_with_no_partner():
    # A lone book whose bid is above its ask is a feed glitch, not free money.
    members = [member("polymarket_us", "pm", "yes")]
    quotes = {("polymarket_us", "pm"): quote("polymarket_us", "pm", "t", [[0.46, 100]], [[0.36, 100]])}
    assert pricing.best_trade(members, quotes, {("polymarket_us", "pm"): NO_PM_FEES}) is None


def test_best_trade_returns_none_without_two_quoted_sides():
    members = [member("kalshi", "k")]
    quotes = {("kalshi", "k"): quote("kalshi", "k", "t", [[0.53, 100]], [])}
    assert pricing.best_trade(members, quotes, {("kalshi", "k"): NO_K_FEES}) is None


def test_fees_are_charged_once_per_level_on_buys_and_sells():
    kalshi = ("kalshi", {"fee_type": "quadratic", "fee_multiplier": 1})
    assert pricing.sweep([(0.5, 100)], 100, *kalshi) == (100, pytest.approx(50 + 1.75))                  # Not 100 fees of 2 cents.
    assert pricing.sweep([(0.5, 100)], 100, *kalshi, selling=True) == (100, pytest.approx(50 - 1.75))
    # Two levels are two trades, each rounded up to the cent on its own.
    assert pricing.sweep([(0.5, 10), (0.6, 10)], 20, *kalshi) == (20, pytest.approx(5 + 0.18 + 6 + 0.17))


def test_edges_use_the_unrounded_fee_per_contract():
    kalshi = ("kalshi", {"fee_type": "quadratic", "fee_multiplier": 1})
    edge, size, profit = pricing.positive_depth([(0.45, 100)], [(0.47, 100)], kalshi, kalshi)
    expected = 1 - 0.45 - 0.47 - 0.07 * 0.45 * 0.55 - 0.07 * 0.47 * 0.53
    assert edge == pytest.approx(expected)          # About 4.53 cents. Rounding each fee up to 2 cents would have said 4.
    assert (size, profit) == (100, pytest.approx(100 * expected))
