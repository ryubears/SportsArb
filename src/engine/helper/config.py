"""
The settings that decide what the live process does, in one place.

Each is read when it is used, as config.NAME, so engine.run's --set can
change one for a run, and the run logs them all when it starts. What
belongs here is what a run might be tuned by. What a venue's protocol or
fee schedule fixes stays with the code for that venue. A setting only
paper trading reads starts with PAPER_, one only live trading reads starts
with LIVE_, and the rest hold for both.

Override one for a run with --set, for example:
    python3 -m engine.run --sport nfl --set min_edge=0.03 --set min_annual_pct=20
"""

# TRADING, trading/executor.py

MIN_EDGE = 0.05             # Net dollars per contract at the top before orders are sent, and the floor for the deeper levels they sweep.
                            # In-game, 2 to 3 cent edges lost money after hedging.
MIN_ANNUAL_PCT = 50         # The least an edge must return a year on the capital it ties up until the bet pays, before or during a
                            # game or on a future. A 5 cent edge clears it if it pays within about 3.5 months.
FILL_SHARE = 0.5            # The share of visible size at a level a trade asks for. Other takers get the rest.
# A leg on a venue here trades only once its book is current: newer, by the venues' own clocks, than the other leg's last
# change, or else that change is this many seconds old, time for any reaction to it on this venue to reach us. Polymarket US
# books reached us 85 ms after the venue changed them at the median, 160 at the 90th percentile, so a price that moved on
# Kalshi sat next to Polymarket US's old one, and on 2026-09-28 only 4 of 72 orders there filled.
CONFIRM_SECONDS = {"polymarket_us": 0.3}
LIVE_SPORTS = ("nfl", "ncaaf", "mlb")  # The sports live trading takes signals on. Kalshi keeps baseball on exchange shard 3, whose
                                        # cash is its own, so live baseball trades only with cash moved to that shard. Hockey is on
                                        # shard 0 with football, and basketball on 3 with baseball, and both trade on paper only
                                        # until their paper trades have settled.

# PAPER, trading/paper.py and money/paper.py. How paper orders fill, and the paper money.

PAPER_REJECT_PROBABILITY = 0.03     # The share of orders a venue rejects outright, for rate limits and errors.
# Signal to fill latency per venue, as median milliseconds and the sigma of a lognormal draw. From us-east-1 a signed
# request round trip is about 35 ms to Kalshi and 30 ms to Polymarket US, and on top of that sit the feed's own lag
# in showing us the book and the venue's matching, so the medians are set above the round trips.
PAPER_LATENCY_MS = {"kalshi": (50, 0.35), "polymarket_us": (60, 0.35)}
PAPER_START_BALANCE = 10000.0   # Paper dollars per venue at the start.

# LIVE, trading/live.py, trading/brakes.py, and money/live.py. Real money, so each limit is kept small until the live results
# earn more. Sized for a test with about 100 dollars on each venue.

LIVE_MAX_CAP = 5            # The most contracts one live trade may hold, whatever the books and the cash would allow.
LIVE_BALANCE_SECONDS = 15   # Between readings of the venues' balances. Under http.IDLE_SECONDS, so each reading also keeps the
                            # venue's kept connection open for the next order: one opened afresh took Polymarket US 11 ms longer.
LIVE_LOW_CASH = 5.0         # Dollars on a live venue under which a human is emailed, once until it is back over. Trades there go on as
                            # far as the cash pays for.
LIVE_ORDER_WINDOW = 20      # The newest orders the unknown outcome brake looks at.
LIVE_UNKNOWN_LIMIT = 3      # Orders with an unknown outcome among the newest LIVE_ORDER_WINDOW at which live trading halts.
LIVE_REJECT_LIMIT = 3       # Orders one venue refuses in a row at which live trading halts.
LIVE_RESULT_HOURS = 6       # The sliding window the loss brake looks at, in hours.
LIVE_MAX_LOSS_SHARE = 0.10  # Net loss of the trades decided in the window, as a share of the live money, over which live trading halts.
KEY_CHECK_HOURS = 1         # Between readings of when the Kalshi key's location attestation lapses, see notify.AttestationWatch.
KEY_WARN_HOURS = 48         # How long before it lapses the email goes out, so it comes before the day it does.

# GAMES, game.py, which the recorder, the scoreboard, the scanner, and the executor time games by. Game
# lengths are from the games Polymarket US has recorded as finished, the settling time from the first live game, Atlanta
# at Green Bay.

GAME_HOURS = {              # How long a game is expected to last, kickoff to final whistle: three in four of the sport's games end
    "nfl": 3.25,            # by then. 168 NFL games had a median of 3.11 hours, three in four by 3.24, and 382 college games a median
    "ncaaf": 3.75,          # of 3.48, three in four by 3.71. 2,399 baseball games had a median of 2.83, three in four by 3.09,
    "mlb": 3.25,            # 475 hockey games a median of 2.75, three in four by 2.88, and 418 basketball games a median of 2.53,
    "nhl": 3.0,             # three in four by 2.71, and all three get more for playoff games, which run longer: three in four of 79
    "nba": 3.0,             # hockey playoff games ended by 3.17, of 96 basketball ones by 2.87. Trading follows the scoreboard, to
}                           # the real end of each game, so this is the end only when Polymarket US says nothing.
SETTLE_HOURS = 0.5          # Final whistle to the venues settling. A game's money is back SETTLE_HOURS after it ends.
RECORD_HOURS = 5            # Kickoff to when a game's contracts stop being recorded, whatever their close time says. The kickoff
                            # is Polymarket US's, since Kalshi gives none, and holds for both venues' contracts.

# RECORDING, market/record.py

BOOK_LEVELS = 5             # Price levels kept per side.
FEED_PROCESSES = True       # Run each venue's feed in a process of its own, see market/feeds.py. False runs every feed in the main process.
GAME_WINDOW_DAYS = 7        # Games further out than this are not recorded.

# SCANNING, market/scan.py and pricing.py

MAX_BOOK_AGE = 60           # Seconds. A book older than this is neither priced nor traded, see pricing.fresh(): its market may have closed.
TARGET_ANNUAL_PCT = 10      # The return an opportunity must beat to be worth the risk.
LOG_PROFIT_DOLLARS = 10     # Live episodes worth at least this at the peak are logged as they end.

# TIMERS, run.py, money/settle.py, and market/scoreboard.py

TICK_SECONDS = 1.0          # How often the session ticks, pricing open episodes again and running each desk.
STATUS_SECONDS = 60         # How often a status line is logged.
SUMMARY_SECONDS = 600       # How often each component logs its summary.
SETTLE_CHECK_SECONDS = 30   # Between passes over the open trades whose game has started.
SCOREBOARD_SECONDS = 30     # Between asking Polymarket US how the games under way stand.


def override(assignments):
    """
    Apply NAME=VALUE assignments for this run, with names in any case. The
    value is read as the setting's own type, so a setting that is a whole
    number stays one, and one that is true or false takes true or false.
    Raises ValueError for an unknown setting, a value of the wrong type, or
    a setting that is not a single number or true or false.
    """
    for assignment in assignments:
        name, sep, text = assignment.partition("=")
        name = name.strip().upper()
        if not sep or not name.isidentifier() or name not in globals() or not name.isupper():
            raise ValueError(f"unknown setting in {assignment!r}")
        current = globals()[name]
        if isinstance(current, bool):
            value = {"true": True, "false": False}.get(text.strip().lower())
            if value is None:
                raise ValueError(f"{name} needs true or false, not {text.strip()!r}")
        elif not isinstance(current, (int, float)):
            raise ValueError(f"{name} is not a single number and cannot be set from the command line")
        else:
            try:
                value = type(current)(text.strip())
            except ValueError:
                kind = "a whole number" if isinstance(current, int) else "a number"
                raise ValueError(f"{name} needs {kind}, not {text.strip()!r}") from None
        globals()[name] = value
