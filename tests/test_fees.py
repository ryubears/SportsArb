"""
Tests for the fee formulas against the venues' documented examples.
"""

import fees
import pytest

PM_SPORTS = {"feesEnabled": True, "feeSchedule": {"rate": 0.05}}
KALSHI_SPORTS = {"fee_type": "quadratic", "fee_multiplier": 1}
KALSHI_WITH_MAKER = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def test_polymarket_peaks_at_documented_amount():
    # Polymarket documents 1.25 dollars per 100 shares at 50 cents on sports markets.
    assert fees.polymarket_fee(0.5, 100, PM_SPORTS) == 1.25


def test_polymarket_is_symmetric_around_even_odds():
    assert fees.polymarket_fee(0.3, 100, PM_SPORTS) == pytest.approx(fees.polymarket_fee(0.7, 100, PM_SPORTS))


def test_kalshi_honors_a_zero_multiplier():
    assert fees.kalshi_fee(0.5, 100, {"fee_type": "quadratic", "fee_multiplier": 0}) == 0.0


def test_polymarket_charges_nothing_when_fees_are_off():
    assert fees.polymarket_fee(0.5, 100, {"feesEnabled": False}) == 0.0
    assert fees.polymarket_fee(0.5, 100, None) == 0.0


def test_kalshi_peaks_at_documented_amount():
    # Kalshi documents 1.75 cents per contract at 50 cents, so 1.75 dollars per 100.
    assert fees.kalshi_fee(0.5, 100, KALSHI_SPORTS) == 1.75


def test_kalshi_rounds_up_to_the_cent():
    assert fees.kalshi_fee(0.5, 1, KALSHI_SPORTS) == 0.02
    assert fees.kalshi_fee(0.1, 1, KALSHI_SPORTS) == 0.01


def test_kalshi_maker_fee_only_on_series_that_charge_it():
    assert fees.kalshi_fee(0.5, 100, KALSHI_SPORTS, maker=True) == 0.0
    assert fees.kalshi_fee(0.5, 100, KALSHI_WITH_MAKER, maker=True) == 0.44


def test_fee_dispatches_by_venue():
    assert fees.fee("polymarket", 0.5, 100, PM_SPORTS) == 1.25
    assert fees.fee("kalshi", 0.5, 100, KALSHI_SPORTS) == 1.75


def test_polymarket_us_rounds_half_to_even():
    assert fees.polymarket_us_fee(0.5, 100, {"feeCoefficient": 0.0695}) == 1.74
    assert fees.polymarket_us_fee(0.5, 100, {}) == 1.74
    assert fees.fee("polymarket_us", 0.5, 100, {"feeCoefficient": 0.0695}) == 1.74
