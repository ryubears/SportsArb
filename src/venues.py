"""
The venues this project knows, and which of them the user can trade on.

Every venue is fetched and recorded, because more books mean more
information about where a bet is mispriced. Only the tradable venues
can carry a leg of a real trade. The scanner and the summary report
both views, everything the markets offer and what can actually be taken.
"""

VENUES = ("polymarket", "kalshi", "polymarket_us")

# The user is a US resident. Polymarket.com does not serve US persons, so it is data only.
TRADABLE_VENUES = ("kalshi", "polymarket_us")

SHORT_NAMES = {"polymarket": "PM", "kalshi": "K", "polymarket_us": "PMUS"}


def is_tradable(venue):
    """
    Whether the user can place orders on this venue.
    """
    return venue in TRADABLE_VENUES
