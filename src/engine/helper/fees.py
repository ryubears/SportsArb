"""
Trading fees for each venue, from their published fee schedules.

Both venues charge takers a fee that peaks at even odds and falls to
zero near certainty, on buys and sells alike. The fee is rounded to the
cent once per order, so fee() takes the whole order's contracts. Pricing
an edge per contract uses fee_per_contract(), which is not rounded.

Kalshi, kalshi.com fee schedule:
    taker fee = round up to the cent of multiplier * 0.07 * contracts * price * (1 - price).
    maker fee = the same with 0.0175, only on series with fee_type quadratic_with_maker_fees.
    The multiplier is stored per contract in fee_info, 1 for sports.

Polymarket US, docs.polymarket.us, fees page:
    taker fee = coefficient * contracts * price * (1 - price), rounded half to even to the cent.
    The coefficient is stored per contract in fee_info as feeCoefficient, 0.0695 for sports.
"""

import math

KALSHI_TAKER_RATE = 0.07
KALSHI_MAKER_RATE = 0.0175
POLYMARKET_US_TAKER_RATE = 0.0695


def kalshi_rate(fee_info, maker=False):
    """
    Kalshi's fee coefficient for a contract: the multiplier times the taker
    rate, or the maker rate on series that charge makers, else nothing.
    """
    multiplier = (fee_info or {}).get("fee_multiplier")
    if multiplier is None:
        multiplier = 1
    if maker:
        if (fee_info or {}).get("fee_type") != "quadratic_with_maker_fees":
            return 0.0
        return multiplier * KALSHI_MAKER_RATE
    return multiplier * KALSHI_TAKER_RATE


def polymarket_us_rate(fee_info):
    """
    Polymarket US's taker fee coefficient for a contract.
    """
    coefficient = (fee_info or {}).get("feeCoefficient")
    return POLYMARKET_US_TAKER_RATE if coefficient is None else coefficient


def kalshi_fee(price, contracts, fee_info, maker=False):
    """
    Fee in dollars for trading contracts at price on Kalshi in one order, rounded up to the cent.
    """
    cents = kalshi_rate(fee_info, maker) * contracts * price * (1 - price) * 100
    # Drop floating point residue first, so an exact number of cents is not pushed up by one.
    return math.ceil(round(cents, 6)) / 100


def polymarket_us_fee(price, contracts, fee_info):
    """
    Taker fee in dollars for trading contracts at price on Polymarket US in one order, rounded half to even to the cent.
    """
    return round(polymarket_us_rate(fee_info) * contracts * price * (1 - price), 2)


FEES = {"kalshi": kalshi_fee, "polymarket_us": polymarket_us_fee}               # The taker fee of each venue, for a whole order.
RATES = {"kalshi": kalshi_rate, "polymarket_us": polymarket_us_rate}            # The taker fee coefficient of each venue.


def fee(venue, price, contracts, fee_info):
    """
    Taker fee in dollars for an order on any venue, rounded as the venue rounds it.
    """
    return FEES[venue](price, contracts, fee_info)


def fee_per_contract(venue, price, fee_info):
    """
    Taker fee in dollars per contract before rounding, which is what each
    contract of a large order adds. Used to price edges, where rounding a
    single contract's fee to the cent would overstate it by up to a cent.
    """
    return RATES[venue](fee_info) * price * (1 - price)
