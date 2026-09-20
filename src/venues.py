"""
The venues this project knows, and which of them the user can trade on.

Every venue is fetched and recorded. Only the tradable venues can carry a
leg of a real trade. The scanner and the summary report both views, so a
reference only venue can be added later without changing them.
"""

VENUES = ("kalshi", "polymarket_us")

# The user is a US resident, so both venues are tradable. Polymarket.com is not offered to US persons.
TRADABLE_VENUES = ("kalshi", "polymarket_us")

SHORT_NAMES = {"kalshi": "K", "polymarket_us": "PMUS"}


def is_tradable(venue):
    """
    Whether the user can place orders on this venue.
    """
    return venue in TRADABLE_VENUES
