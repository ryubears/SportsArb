"""
The settings that decide what the live process does, in one place.

Each is read when it is used, as config.NAME, so live.run's --set can
change one for a run, and the run logs them all when it starts. What
belongs here is what a run might be tuned by. What a venue's protocol or
fee schedule fixes stays with the code for that venue.

Override one for a run with --set, for example:
    python3 -m live.run --sport nfl --set min_edge=0.03 --set max_cap=100
"""

# TRADING, execute/executor.py and execute/paper.py

MIN_EDGE = 0.05             # Net dollars per contract at the top before orders are sent, and the floor for the deeper levels they sweep.
                            # In-game, 2 to 3 cent edges lost money after hedging.
FILL_SHARE = 0.5            # The share of visible size at a level assumed to be ours. Other takers get the rest.
REJECT_PROBABILITY = 0.03   # The share of orders a venue rejects outright, for rate limits and errors.
# Signal to fill latency per venue, as median milliseconds and the sigma of a lognormal draw. From us-east-1 a signed
# request round trip is about 35 ms to Kalshi and 30 ms to Polymarket US, and on top of that sit the feed's own lag
# in showing us the book and the venue's matching, so the medians are set above the round trips.
LATENCY_MS = {"kalshi": (50, 0.35), "polymarket_us": (60, 0.35)}

# SIZING, allocate.py

DOLLARS_PER_CAP = 20        # Dollars a game spends on each venue, over the whole game, for every contract of cap. From the first live game.
MIN_CAP = 5                 # Contracts per trade, the least worth sending.
MAX_CAP = 500               # Contracts per trade, the most one trade may hold.

# MONEY, balances.py and rebalance.py

START_BALANCE = 10000.0     # Paper dollars per venue at the start.
REBALANCE_WEEKDAY = 1       # Tuesday in UTC, when balances are compared, once Monday night's trades have settled.
REBALANCE_DRIFT = 0.25      # A venue this far above the two venue average on the weekly check sends the excess over.
REBALANCE_FLOOR = 500.0     # A venue below this is topped up to the average on any day.
TRANSFER_DAYS = 4           # Business days a transfer between venues takes.

# GAMES, game.py. Measured on the first live game, Atlanta at Green Bay.

GAME_HOURS = 3.25           # Kickoff to final whistle, with a little margin over the 3.05 measured.
SETTLE_HOURS = 0.5          # Final whistle to the venues settling. A game pays out GAME_HOURS + SETTLE_HOURS after kickoff.
RECORD_HOURS = 5            # Kickoff to when a game's contracts stop being recorded, whatever their close time says.

# RECORDING, record.py

BOOK_LEVELS = 5             # Price levels kept per side.
GAME_WINDOW_DAYS = 7        # Games further out than this are not recorded.

# SCANNING, scan.py

MAX_QUOTE_AGE = 60          # Seconds. A member whose newest book is older than this is left out, it may be stale.
TARGET_ANNUAL_PCT = 10      # The return an opportunity must beat to be worth the risk.
LOG_PROFIT_DOLLARS = 10     # Live episodes worth at least this at the peak are logged as they end.

# TIMERS, run.py and settle.py

FLUSH_SECONDS = 1.0         # How often changed books are written.
STATUS_SECONDS = 60         # How often a status line is logged.
SUMMARY_SECONDS = 600       # How often each component logs its summary.
SETTLE_CHECK_SECONDS = 30   # Between passes over the open trades whose game has started.


def override(assignments):
    """
    Apply NAME=VALUE assignments for this run, with names in any case. The
    value is read as the setting's own type, so a setting that is a whole
    number stays one. Raises ValueError for an unknown setting, a value of
    the wrong type, or a setting that is not a single number.
    """
    for assignment in assignments:
        name, sep, text = assignment.partition("=")
        name = name.strip().upper()
        if not sep or not name.isidentifier() or name not in globals() or not name.isupper():
            raise ValueError(f"unknown setting in {assignment!r}")
        current = globals()[name]
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(f"{name} is not a single number and cannot be set from the command line")
        try:
            value = type(current)(text.strip())
        except ValueError:
            kind = "a whole number" if isinstance(current, int) else "a number"
            raise ValueError(f"{name} needs {kind}, not {text.strip()!r}") from None
        globals()[name] = value
