"""
What a venue answered to an order, in the same words for every venue.

Each venue client's place_order() returns an Answer, so the live executor
never reads a venue's own field names.
"""

from typing import NamedTuple


class Answer(NamedTuple):
    """
    What came back for one immediate or cancel order.
    """
    order_id: str | None    # The venue's id for the order, None when it never took it.
    status: str             # 'filled', 'partial', 'unfilled', 'rejected' when the venue refused it, or 'error' when we cannot tell what happened.
    filled: int             # Contracts bought or sold.
    dollars: float          # Paid for a buy or received for a sale, fees included.
    fees: float
    note: str | None        # Why the venue refused the order, or the error, when there is one.
    response: dict          # The venue's answer as it came, for reconciling.


def status(filled, quantity):
    """
    The status of an order the venue took, from how much of it filled.
    """
    return "filled" if filled >= quantity else "partial" if filled else "unfilled"


def unknown(error):
    """
    The Answer for an order whose fate we cannot know: it timed out, the
    connection dropped, or the venue failed on its side. It may have traded.
    """
    return Answer(None, "error", 0, 0.0, 0.0, repr(error)[:500], {"error": repr(error)})


def refused(error):
    """
    The Answer for an order the venue refused, with the reason it gave.
    """
    return Answer(None, "rejected", 0, 0.0, 0.0, error.body[:500], {"error": error.body, "status": error.status})
