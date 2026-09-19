"""
Trading fees for each venue, from their published fee schedules.

Both venues charge takers a fee that peaks at even odds and falls to
zero near certainty. The functions return dollars for a whole order.

Polymarket, docs.polymarket.com, fees page:
    fee = shares * rate * price * (1 - price), makers pay nothing.
    The rate is stored per contract in fee_info, 0.03 or 0.05 for sports.

Kalshi, kalshi.com fee schedule:
    taker fee = round up to the cent of multiplier * 0.07 * contracts * price * (1 - price).
    maker fee = the same with 0.0175, only on series with fee_type quadratic_with_maker_fees.
    The multiplier is stored per contract in fee_info, 1 for sports.
"""

import math

KALSHI_TAKER_RATE = 0.07
KALSHI_MAKER_RATE = 0.0175


def polymarket_fee(price, contracts, fee_info):
    """
    Taker fee in dollars for buying contracts at price on Polymarket.
    """
    if not fee_info or not fee_info.get("feesEnabled"):
        return 0.0
    rate = (fee_info.get("feeSchedule") or {}).get("rate", 0.0)
    return contracts * rate * price * (1 - price)


def kalshi_fee(price, contracts, fee_info, maker=False):
    """
    Fee in dollars for buying contracts at price on Kalshi, rounded up to the cent.
    """
    multiplier = (fee_info or {}).get("fee_multiplier") or 1
    if maker:
        if (fee_info or {}).get("fee_type") != "quadratic_with_maker_fees":
            return 0.0
        rate = KALSHI_MAKER_RATE
    else:
        rate = KALSHI_TAKER_RATE
    return math.ceil(multiplier * rate * contracts * price * (1 - price) * 100) / 100


def fee(venue, price, contracts, fee_info):
    """
    Taker fee in dollars for either venue.
    """
    if venue == "polymarket":
        return polymarket_fee(price, contracts, fee_info)
    return kalshi_fee(price, contracts, fee_info)
