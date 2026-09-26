"""
Price holding one side of a bet through a contract, and buying both sides
of a bet across venues.

A contract's book is seen from the side it pays on. Holding that side
means buying at the asks. Holding the other side means buying the
opposite outcome, which costs one minus the bid. Costs include the taker
fee of the venue, from fees.py. Shared by the scanner, which looks for a
positive net edge, and the executor, which fills against the same ladders.
"""

from typing import NamedTuple
from common.venues import SHORT_NAMES
from live.price import fees


class Priced(NamedTuple):
    """
    The best way to buy both sides of a bet right now.
    """
    yes: dict           # The pair member holding the yes leg.
    no: dict            # The pair member holding the no leg.
    edge: float         # Net dollars per contract at the top of both books.
    size: float         # Contracts fillable at a positive net edge, walking both ladders.
    profit: float       # Net dollars from filling size.


def ladder(quote, polarity, side):
    """
    Cost per contract and size for each level of holding one side of the
    bet through this contract, cheapest first. Holding the side the contract
    pays on means buying it at its asks. Holding the other side means buying
    the opposite outcome, which costs one minus the bid.
    """
    if side == polarity:
        return [(price, size) for price, size in quote.asks]
    return [(round(1 - price, 4), size) for price, size in quote.bids]


def sell_ladder(quote, polarity, side):
    """
    Proceeds per contract and size for selling back one side of the bet held
    through this contract, best first. Holding the side the contract pays
    on means selling at the bids. Holding the other side means selling the
    opposite outcome, which fetches one minus the ask.
    """
    if side == polarity:
        return [(price, size) for price, size in quote.bids]
    return [(round(1 - price, 4), size) for price, size in quote.asks]


def sweep(levels, quantity, venue, fee_info, share=1.0, limit=None, selling=False):
    """
    What an order for quantity contracts gets from one ladder, best level
    first, as (contracts, dollars). Only share of each level's size is
    taken, whole contracts only, so a level too small for one is skipped
    and the next may still fill. A buy stops at levels priced above limit.
    Dollars include the venue's fee, charged on buys and sells alike and
    rounded once per level: added to what a buy pays, and deducted from
    what a sale brings in.
    """
    filled, dollars, remaining = 0, 0.0, quantity
    for price, size in levels:
        if limit is not None and price > limit + 1e-9:
            break
        take = int(min(remaining, size * share))
        if take < 1:
            continue
        fee = fees.fee(venue, price, take, fee_info)      # Each level fills as one trade, with its fee rounded once.
        filled += take
        dollars += take * price - fee if selling else take * price + fee
        remaining -= take
        if remaining < 1:
            break
    return filled, dollars


def walk_pair(leg_a, leg_b, fee_a, fee_b):
    """
    Walk two ladders together, cheapest first, matching equal amounts of
    each. Yields (cost_a, cost_b, contracts, net edge per contract) for each
    step, with no floor on the edge, so the caller stops where it wants.
    fee_a and fee_b are (venue, fee_info) for each leg.
    """
    i = j = 0
    left_a = left_b = None      # What is left of the current level on each ladder.
    while i < len(leg_a) and j < len(leg_b):
        cost_a, size_a = leg_a[i]
        cost_b, size_b = leg_b[j]
        left_a = size_a if left_a is None else left_a
        left_b = size_b if left_b is None else left_b
        contracts = min(left_a, left_b)
        edge = (1 - cost_a - cost_b - fees.fee_per_contract(fee_a[0], cost_a, fee_a[1])
                - fees.fee_per_contract(fee_b[0], cost_b, fee_b[1]))
        yield cost_a, cost_b, contracts, edge
        left_a -= contracts
        left_b -= contracts
        if left_a <= 0:
            i, left_a = i + 1, None
        if left_b <= 0:
            j, left_b = j + 1, None


def positive_depth(leg_a, leg_b, fee_a, fee_b):
    """
    Buy equal amounts of two ladders while the net edge per contract stays
    positive. Returns (edge at the top, contracts, profit), with an edge of
    -1 when a ladder is empty.
    """
    top_edge, size, profit = None, 0.0, 0.0
    for _, _, contracts, edge in walk_pair(leg_a, leg_b, fee_a, fee_b):
        if top_edge is None:
            top_edge = edge
        if edge <= 0:
            break
        size += contracts
        profit += contracts * edge
    return (top_edge if top_edge is not None else -1.0), size, profit


def depth(leg_a, leg_b, fee_a, fee_b, min_edge):
    """
    Buy equal amounts of two ladders while the net edge per contract stays
    at or above min_edge. Returns the deepest cost included on each leg,
    which is the limit an order needs to sweep those levels, and the
    contracts within them: (limit_a, limit_b, contracts).
    """
    limit_a = limit_b = None
    total = 0.0
    for cost_a, cost_b, contracts, edge in walk_pair(leg_a, leg_b, fee_a, fee_b):
        if edge < min_edge:
            break
        limit_a, limit_b = cost_a, cost_b
        total += contracts
    return limit_a, limit_b, total


def cheapest(members, quotes, side, fee_infos):
    """
    The member offering the lowest fee inclusive cost at the top of book to
    hold one side of the bet. Returns (member, cost) or (None, None).
    """
    best, best_cost = None, None
    for m in members:
        levels = ladder(quotes[(m["venue"], m["contract_id"])], m["polarity"], side)
        if not levels:
            continue
        cost = levels[0][0] + fees.fee_per_contract(m["venue"], levels[0][0], fee_infos[(m["venue"], m["contract_id"])])
        if best_cost is None or cost < best_cost:
            best, best_cost = m, cost
    return best, best_cost


def price_pair(yes, no, quotes, fee_infos):
    """
    Price buying the yes leg and the no leg together.
    """
    yes_key, no_key = (yes["venue"], yes["contract_id"]), (no["venue"], no["contract_id"])
    edge, size, profit = positive_depth(ladder(quotes[yes_key], yes["polarity"], "yes"), ladder(quotes[no_key], no["polarity"], "no"),
                                        (yes["venue"], fee_infos[yes_key]), (no["venue"], fee_infos[no_key]))
    return Priced(yes, no, edge, size, profit)


def best_trade(members, quotes, fee_infos):
    """
    The cheapest yes leg and the cheapest no leg across a group's members,
    priced together. The two legs are never the same contract, since buying
    both sides of one book is not a trade between venues and a crossed book
    would look like free money. Returns a Priced, or None when a side has
    no quotes on another contract.
    """
    yes, _ = cheapest(members, quotes, "yes", fee_infos)
    no, _ = cheapest(members, quotes, "no", fee_infos)
    if yes is None or no is None:
        return None
    if yes is not no:
        return price_pair(yes, no, quotes, fee_infos)
    # One contract is cheapest on both sides. Try the best partner for each side and keep the better pair.
    others = [m for m in members if m is not yes]
    candidates = []
    other_no, _ = cheapest(others, quotes, "no", fee_infos)
    if other_no is not None:
        candidates.append(price_pair(yes, other_no, quotes, fee_infos))
    other_yes, _ = cheapest(others, quotes, "yes", fee_infos)
    if other_yes is not None:
        candidates.append(price_pair(other_yes, no, quotes, fee_infos))
    return max(candidates, key=lambda c: c.edge) if candidates else None


def trade_words(yes, no):
    """
    The two legs in words, for example 'yes: K buy, no: PMUS buy other side'.
    """
    def leg(member, side):
        action = "buy" if member["polarity"] == side else "buy other side"
        return f"{side}: {SHORT_NAMES.get(member['venue'], member['venue'])} {action}"
    return f"{leg(yes, 'yes')}, {leg(no, 'no')}"
