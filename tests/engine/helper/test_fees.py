"""
Tests for the fee formulas against the venues' documented examples.
"""

import pytest
from engine.helper import fees

KALSHI_SPORTS = {"fee_type": "quadratic", "fee_multiplier": 1}
KALSHI_WITH_MAKER = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def test_kalshi_peaks_at_documented_amount():
    # Kalshi documents 1.75 cents per contract at 50 cents, so 1.75 dollars per 100.
    assert fees.kalshi_fee(0.5, 100, KALSHI_SPORTS) == 1.75


def test_kalshi_rounds_up_to_the_hundredth_of_a_cent_as_it_charged_our_orders():
    assert fees.kalshi_fee(0.5, 1, KALSHI_SPORTS) == 0.0175
    assert fees.kalshi_fee(0.1, 1, KALSHI_SPORTS) == 0.0063
    # What live orders paid: one contract at 19 cents, 0.51 at 45 cents, 0.01 at 19, 9, and 63 cents, 0.2 at 3.3 cents.
    assert fees.kalshi_fee(0.19, 1, KALSHI_SPORTS) == 0.0108
    assert fees.kalshi_fee(0.45, 0.51, KALSHI_SPORTS) == 0.0089
    assert fees.kalshi_fee(0.19, 0.01, KALSHI_SPORTS) == 0.0002
    assert fees.kalshi_fee(0.09, 0.01, KALSHI_SPORTS) == 0.0001
    assert fees.kalshi_fee(0.63, 0.04, KALSHI_SPORTS) == 0.0007
    assert fees.kalshi_fee(0.033, 0.2, KALSHI_SPORTS) == 0.0005
    # An exact number of hundredths of a cent is not pushed up by floating point residue.
    assert fees.kalshi_fee(0.2, 1, KALSHI_SPORTS) == 0.0112


def test_kalshi_maker_fee_only_on_series_that_charge_it():
    assert fees.kalshi_fee(0.5, 100, KALSHI_SPORTS, maker=True) == 0.0
    assert fees.kalshi_fee(0.5, 100, KALSHI_WITH_MAKER, maker=True) == 0.4375


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
