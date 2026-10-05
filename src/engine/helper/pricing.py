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
from common.timeutil import seconds_between
from common.venues import SHORT_NAMES
from engine.helper import config, fees


class Priced(NamedTuple):
    """
    The best way to buy both sides of a bet right now.
    """
    yes: dict           # The pair member holding the yes leg.
    no: dict            # The pair member holding the no leg.
    edge: float         # Net dollars per contract at the top of both books.
    size: float         # Contracts fillable at a positive net edge, walking both ladders.
    profit: float       # Net dollars from filling size.
    min_edge_size: float = 0.0      # Contracts fillable at config.MIN_EDGE or more, the levels an order would sweep.
    min_edge_profit: float = 0.0    # Net dollars from filling min_edge_size.


def fresh(book, now, aging=True):
    """
    Whether a book can be priced and traded at now: it has changed within
    config.MAX_BOOK_AGE seconds. A market that has closed may stop changing
    rather than empty its book, and its last book cannot be traded, so an
    old book counts as no book. A quiet market that is still open waits for
    its next change. A book that may rest unchanged for hours while its
    market is open, as a future's does, or a game's before kickoff, is priced
    without aging: it counts until the recorder stops following it.
    """
    return book is not None and (not aging or seconds_between(book.ts, now) <= config.MAX_BOOK_AGE)


def ladder(book, polarity, side):
    """
    Cost per contract and size for each level of holding one side of the
    bet through this contract, cheapest first. Holding the side the contract
    pays on means buying it at its asks. Holding the other side means buying
    the opposite outcome, which costs one minus the bid.
    """
    if side == polarity:
        return [(price, size) for price, size in book.asks]
    return [(round(1 - price, 4), size) for price, size in book.bids]


def sell_ladder(book, polarity, side):
    """
    Proceeds per contract and size for selling back one side of the bet held
    through this contract, best first. Holding the side the contract pays
    on means selling at the bids. Holding the other side means selling the
    opposite outcome, which fetches one minus the ask.
    """
    if side == polarity:
        return [(price, size) for price, size in book.bids]
    return [(round(1 - price, 4), size) for price, size in book.asks]


def book_level(polarity, side, price, selling=False):
    """
    The level of the contract's book that a level of ladder() came from, or
    of sell_ladder() when selling, as (book side, book price). The side the
    contract pays on is bought at the asks and sold at the bids, and the
    other side the other way round, at one minus the book's price.
    """
    own = side == polarity
    return "asks" if own != selling else "bids", round(price if own else 1 - price, 4)


def takes(levels, quantity, share=1.0, limit=None, step=1):
    """
    The contracts an order for quantity takes from each level of one ladder,
    best level first, as (price, contracts). Only share of each level's
    size is taken, in whole steps, whole contracts unless step says less, so
    a level too small for one step is skipped and the next may still fill.
    A buy stops at levels priced above limit.
    """
    remaining = quantity
    for price, size in levels:
        if limit is not None and price > limit + 1e-9:
            break
        take = round(int(min(remaining, size * share) / step + 1e-9) * step, 2)
        if take < step:
            continue
        yield price, take
        remaining = round(remaining - take, 2)
        if remaining < step:
            break


def sweep(levels, quantity, venue, fee_info, share=1.0, limit=None, selling=False, step=1):
    """
    What an order for quantity contracts gets from one ladder, as
    (contracts, dollars), taking from the levels in steps as takes() does. Dollars
    include the venue's fee, charged on buys and sells alike and rounded
    once per level: added to what a buy pays, and deducted from what a
    sale brings in.
    """
    filled, dollars = 0, 0.0
    for price, take in takes(levels, quantity, share, limit, step):
        fee = fees.fee(venue, price, take, fee_info)      # Each level fills as one trade, with its fee rounded once.
        filled = round(filled + take, 2)
        dollars += take * price - fee if selling else take * price + fee
    return filled, dollars


def reach(levels, quantity, share=1.0, step=1):
    """
    The price of the deepest level an order for quantity contracts takes
    from, as takes() walks the ladder in steps, or None when it takes
    nothing. For a buy it is the highest price paid, for a sale the lowest
    price accepted, so it is the limit that lets a real order do what the
    sweep did.
    """
    price = None
    for price, _ in takes(levels, quantity, share, step=step):
        pass
    return price


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


def positive_depth(leg_a, leg_b, fee_a, fee_b, min_edge):
    """
    Buy equal amounts of two ladders while the net edge per contract stays
    positive. Returns (edge at the top, contracts, profit, contracts and
    profit of the levels at min_edge or more), with an edge of -1 when a
    ladder is empty.
    """
    top_edge, size, profit, worth_size, worth_profit = None, 0.0, 0.0, 0.0, 0.0
    worth = True        # Still among the levels at min_edge or more, which run from the top down.
    for _, _, contracts, edge in walk_pair(leg_a, leg_b, fee_a, fee_b):
        if top_edge is None:
            top_edge = edge
        if edge <= 0:
            break
        size += contracts
        profit += contracts * edge
        worth = worth and edge >= min_edge
        if worth:
            worth_size, worth_profit = size, profit
    return (top_edge if top_edge is not None else -1.0), size, profit, worth_size, worth_profit


def depth(leg_a, leg_b, fee_a, fee_b, min_edge, most=None):
    """
    Buy equal amounts of two ladders while the net edge per contract stays
    at or above min_edge, and, given most, only until most contracts are
    reached. Returns the deepest cost included on each leg, which is the
    limit an order needs to sweep those levels, and the contracts within
    them: (limit_a, limit_b, contracts).
    """
    limit_a = limit_b = None
    total = 0.0
    for cost_a, cost_b, contracts, edge in walk_pair(leg_a, leg_b, fee_a, fee_b):
        if edge < min_edge or (most is not None and total >= most):
            break
        limit_a, limit_b = cost_a, cost_b
        total += contracts
    return limit_a, limit_b, total


def cheapest(members, books, side, fee_infos):
    """
    The member offering the lowest fee inclusive cost at the top of book to
    hold one side of the bet. Returns (member, cost) or (None, None).
    """
    best, best_cost = None, None
    for m in members:
        levels = ladder(books[(m["venue"], m["contract_id"])], m["polarity"], side)
        if not levels:
            continue
        cost = levels[0][0] + fees.fee_per_contract(m["venue"], levels[0][0], fee_infos[(m["venue"], m["contract_id"])])
        if best_cost is None or cost < best_cost:
            best, best_cost = m, cost
    return best, best_cost


def ladders(yes, no, books, fee_infos):
    """
    The yes leg's and the no leg's ladders, then their (venue, fee_info), as positive_depth() takes them.
    """
    yes_key, no_key = (yes["venue"], yes["contract_id"]), (no["venue"], no["contract_id"])
    return (ladder(books[yes_key], yes["polarity"], "yes"), ladder(books[no_key], no["polarity"], "no"),
            (yes["venue"], fee_infos[yes_key]), (no["venue"], fee_infos[no_key]))


def price_pair(yes, no, books, fee_infos):
    """
    Price buying the yes leg and the no leg together.
    """
    return Priced(yes, no, *positive_depth(*ladders(yes, no, books, fee_infos), config.MIN_EDGE))


def fillable(yes, no, books, fee_infos, min_edge):
    """
    The contracts fillable buying the yes leg and the no leg together on the levels at min_edge or more, from the top
    down, and the net dollars they lock in: what an order sweeping those levels would have had.
    """
    return positive_depth(*ladders(yes, no, books, fee_infos), min_edge)[3:]


def best_trade(members, books, fee_infos):
    """
    The cheapest yes leg and the cheapest no leg across a pair's members,
    on two different venues, priced together. Both legs are never on one
    venue: buying both sides of one book is not a trade, a crossed book would
    look like free money, and two contracts on one venue, such as a game's
    two teams on Kalshi, are priced by the same traders, so a gap between
    them is one that moved before the other and is gone before both orders
    land. In the in-play test of 2026-10-04 52 trades with both legs on
    Kalshi matched 5 of the 247 contracts they asked for, live and paper
    alike. Returns a Priced, or None when no two venues quote a side each.
    """
    yes, _ = cheapest(members, books, "yes", fee_infos)
    no, _ = cheapest(members, books, "no", fee_infos)
    if yes is None or no is None:
        return None
    if yes["venue"] != no["venue"]:
        return price_pair(yes, no, books, fee_infos)
    # One venue is cheapest on both sides. Try the best partner on another venue for each side and keep the better pair.
    candidates = []
    other_no, _ = cheapest([m for m in members if m["venue"] != yes["venue"]], books, "no", fee_infos)
    if other_no is not None:
        candidates.append(price_pair(yes, other_no, books, fee_infos))
    other_yes, _ = cheapest([m for m in members if m["venue"] != no["venue"]], books, "yes", fee_infos)
    if other_yes is not None:
        candidates.append(price_pair(other_yes, no, books, fee_infos))
    return max(candidates, key=lambda c: c.edge) if candidates else None


def trade_words(yes, no):
    """
    The two legs in words, for example 'yes: K buy, no: PMUS buy other side'.
    """
    def leg(member, side):
        action = "buy" if member["polarity"] == side else "buy other side"
        return f"{side}: {SHORT_NAMES.get(member['venue'], member['venue'])} {action}"
    return f"{leg(yes, 'yes')}, {leg(no, 'no')}"


def return_pct(edge):
    """
    What an edge returns on the capital it ties up, as a percent: both legs
    cost a dollar less the edge, and together they pay a dollar.
    """
    return 100 * edge / (1 - edge)


def annual_pct(edge, days):
    """
    An edge's return_pct() scaled to a year, without compounding, over the days until the bet pays.
    """
    return return_pct(edge) * 365 / days


def edge_for_annual(pct, days):
    """
    The least edge that returns pct a year over the days until the bet pays, see annual_pct().
    """
    ratio = pct * days / 36500
    return ratio / (1 + ratio)


def polymarket_us_just_changed(yes, no, books, now):
    """
    Whether the Polymarket US leg's book reached us no more than
    config.LIVE_IN_PLAY_PM_SECONDS before now: at 0, whether the pricing at
    now came with that book's own change, not Kalshi's, another member's, a
    recheck, or the tick. True with no leg there. Live trades a game under
    way only then, and the summary counts an episode in play only when its
    peak was such a moment, since in play a price that stands still there
    may be one no order can fill.
    """
    for member in (yes, no):
        if member["venue"] == "polymarket_us":
            book = books.get((member["venue"], member["contract_id"]))
            return book is not None and seconds_between(book.ts, now) <= config.LIVE_IN_PLAY_PM_SECONDS
    return True


def live_min_edge(under_way, days):
    """
    The least edge live takes, and the floor of the deeper levels its orders
    sweep: config.LIVE_IN_PLAY_MIN_EDGE on a game under way, otherwise the
    edge that returns config.MIN_ANNUAL_PCT a year over the days until the
    bet pays, or config.MIN_EDGE when it gives no payout. The live executor
    trades by it, and the scanner keeps what an order at an episode's peak
    could have had by it.
    """
    if under_way:
        return config.LIVE_IN_PLAY_MIN_EDGE
    return config.MIN_EDGE if days is None else edge_for_annual(config.MIN_ANNUAL_PCT, days)
