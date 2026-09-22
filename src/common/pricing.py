"""
Price holding one side of a bet through a contract, and buying both sides
of a bet group across venues.

A contract's book is seen from the side it pays on. Holding that side
means buying at the asks. Holding the other side means buying the
opposite outcome, which costs one minus the bid. Costs include the taker
fee of the venue, from fees.py. Shared by the scanner, which looks for a
positive net edge, and the executor, which fills against the same ladders.
"""

from common import fees
from venues import SHORT_NAMES


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


def fill(leg_a, leg_b, venue_a, venue_b, fee_infos):
    """
    Walk two ladders together, buying equal amounts of each while the net
    edge per contract stays positive. Returns (edge at the top, contracts, profit).
    """
    i = j = 0
    size = profit = 0.0
    top_edge = None
    while i < len(leg_a) and j < len(leg_b):
        cost_a, size_a = leg_a[i]
        cost_b, size_b = leg_b[j]
        qty = min(size_a, size_b)
        edge = (1 - cost_a - cost_b
                - fees.fee(venue_a, cost_a, 1, fee_infos[venue_a])
                - fees.fee(venue_b, cost_b, 1, fee_infos[venue_b]))
        if top_edge is None:
            top_edge = edge
        if edge <= 0:
            break
        size += qty
        profit += qty * edge
        leg_a[i] = (cost_a, size_a - qty)
        leg_b[j] = (cost_b, size_b - qty)
        if leg_a[i][1] <= 0:
            i += 1
        if leg_b[j][1] <= 0:
            j += 1
    return (top_edge if top_edge is not None else -1.0), size, profit


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
        cost = levels[0][0] + fees.fee(m["venue"], levels[0][0], 1, fee_infos[(m["venue"], m["contract_id"])])
        if best_cost is None or cost < best_cost:
            best, best_cost = m, cost
    return best, best_cost


def price_pair(yes, no, quotes, fee_infos):
    """
    Price buying the yes leg and the no leg together. Returns (yes, no, edge at top, size, profit).
    """
    yes_key, no_key = (yes["venue"], yes["contract_id"]), (no["venue"], no["contract_id"])
    edge, size, profit = fill(ladder(quotes[yes_key], yes["polarity"], "yes"), ladder(quotes[no_key], no["polarity"], "no"),
                              yes["venue"], no["venue"], {yes["venue"]: fee_infos[yes_key], no["venue"]: fee_infos[no_key]})
    return yes, no, edge, size, profit


def best_trade(members, quotes, fee_infos):
    """
    The cheapest yes leg and the cheapest no leg across a group's members,
    priced together. The two legs are never the same contract, since buying
    both sides of one book is not a trade between venues and a crossed book
    would look like free money. Returns (yes member, no member, edge at top,
    size, profit), or None when a side has no quotes on another contract.
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
    return max(candidates, key=lambda c: c[2]) if candidates else None


def trade_words(yes, no):
    """
    The two legs in words, for example 'yes: K buy, no: PMUS buy other side'.
    """
    def leg(member, side):
        action = "buy" if member["polarity"] == side else "buy other side"
        return f"{side}: {SHORT_NAMES.get(member['venue'], member['venue'])} {action}"
    return f"{leg(yes, 'yes')}, {leg(no, 'no')}"
