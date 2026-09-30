"""
The pair both executors' tests trade, and the books they trade it against.

Yes is held through the Polymarket US contract and no through the other
side of the Kalshi contract, on a game half an hour past kickoff at NOW.
Neither venue charges a fee, so the tests' arithmetic comes out exact.
"""

from db.models import Book

NO_PM_FEES = {"feeCoefficient": 0}                              # Polymarket US without its taker fee.
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}      # Kalshi without its taker fee.
KICKOFF = "2026-09-20T17:00:00+00:00"                           # When the game kicked off.
NOW = "2026-09-20T17:30:00+00:00"                               # When the tests trade, during the game.
CLOSE = "2026-09-20T21:00:00+00:00"                             # When both contracts close.
PAIR = {"id": 1, "sport": "nfl", "label": "nfl game_winner 2026-09-20 CAR@ATL CAR", "kind": "game_winner",                 # The pair traded.
        "game_date": "2026-09-20", "team_a": "CAR", "team_b": "ATL"}
YES = {"venue": "polymarket_us", "contract_id": "pm", "polarity": "yes", "start_time": KICKOFF, "close_time": CLOSE}  # Holds yes.
NO = {"venue": "kalshi", "contract_id": "k", "polarity": "yes", "start_time": KICKOFF, "close_time": CLOSE}         # Holds no.
FEES = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES}    # Each member's fee schedule.


def books(pm_bid=0.44, pm_ask=0.45, k_bid=0.53, k_ask=0.54, size=100):
    """
    Books where yes is cheapest on Polymarket US at the ask and no is cheapest on Kalshi through the bid.
    """
    return {("polymarket_us", "pm"): Book("polymarket_us", "pm", NOW, [[pm_bid, size]], [[pm_ask, size]]),
            ("kalshi", "k"): Book("kalshi", "k", NOW, [[k_bid, size]], [[k_ask, size]])}


def stored(conn, table="trades"):
    """
    Every row of a table, oldest first, as dicts.
    """
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
