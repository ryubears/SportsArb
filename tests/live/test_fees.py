"""
Tests for the fee formulas against the venues' documented examples.
"""

import pytest
from live import fees

KALSHI_SPORTS = {"fee_type": "quadratic", "fee_multiplier": 1}
KALSHI_WITH_MAKER = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


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
    assert fees.fee("kalshi", 0.5, 100, KALSHI_SPORTS) == 1.75


def test_polymarket_us_rounds_half_to_even():
    assert fees.polymarket_us_fee(0.5, 100, {"feeCoefficient": 0.0695}) == 1.74
    assert fees.polymarket_us_fee(0.5, 100, {}) == 1.74
    assert fees.fee("polymarket_us", 0.5, 100, {"feeCoefficient": 0.0695}) == 1.74


def test_fee_per_contract_is_not_rounded():
    assert fees.fee_per_contract("kalshi", 0.5, KALSHI_SPORTS) == pytest.approx(0.0175)
    assert fees.fee_per_contract("polymarket_us", 0.5, {"feeCoefficient": 0.0695}) == pytest.approx(0.017375)
    assert fees.fee_per_contract("kalshi", 0.5, {"fee_multiplier": 0}) == 0
    # A hundred contracts in one order cost what the per contract rate says, once rounded, not 100 single contract fees.
    assert fees.fee("kalshi", 0.5, 100, KALSHI_SPORTS) == 1.75 < 100 * fees.fee("kalshi", 0.5, 1, KALSHI_SPORTS)
