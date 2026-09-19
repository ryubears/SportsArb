"""
Data classes shared across the project.
"""

from dataclasses import dataclass


@dataclass
class FeeRecord:
    """
    The fee schedule a contract carried from a moment on. A new record is
    written each time a fetch sees the schedule change.
    """
    venue: str
    contract_id: str
    seen_at: str            # The fetch time that first showed this schedule, ISO 8601 UTC.
    fee_info: dict


@dataclass
class Contract:
    """
    One tradable Yes side outcome, described the same way for every venue.
    """
    venue: str
    contract_id: str        # Polymarket outcome token id, or Kalshi market ticker.
    market_id: str          # Polymarket conditionId, or Kalshi market ticker.
    event_id: str           # Polymarket event slug, or Kalshi event ticker.
    series_id: str | None   # Kalshi series ticker. Polymarket has none.
    sport: str
    event_title: str | None
    title: str
    outcome: str            # 'Yes', a team name, 'Over', and so on.
    market_type: str | None
    line: float | None
    rules: str | None
    start_time: str | None
    close_time: str | None
    fee_info: dict | None


@dataclass
class Bet:
    """
    What a contract is about, in venue neutral terms. Two contracts on
    different venues with the same Bet fields describe the same bet.
    """
    venue: str
    contract_id: str
    kind: str               # 'champion', 'game_winner', 'spread', 'total', and so on.
    season: int | None      # The year the season ends, for example 2027.
    game_date: str | None   # Game date in US Eastern time as YYYY-MM-DD, for game kinds only.
    team_a: str | None      # Away team code for game kinds.
    team_b: str | None      # Home team code for game kinds.
    subject: str | None     # The team the contract is about, when there is one.
    line: float | None      # Spread margin, total points, or wins threshold.
    polarity: str           # 'yes' pays when the bet's statement is true, 'no' pays when it is false.


@dataclass
class Pair:
    """
    One Polymarket contract and one Kalshi contract that describe the same bet.
    """
    kind: str
    season: int | None
    game_date: str | None
    team_a: str | None
    team_b: str | None
    subject: str | None
    line: float | None
    polymarket_id: str
    kalshi_id: str
    polymarket_polarity: str    # 'yes' or 'no', see Bet.
    kalshi_polarity: str
    close_gap_days: float | None    # Kalshi close time minus Polymarket close time.
    flags: list[str]                # Things a human should check before trusting the pair.


@dataclass
class Quote:
    """
    One contract's order book at one moment, seen from the Yes side.
    """
    venue: str
    contract_id: str
    ts: str                 # Our clock, ISO 8601 UTC, when the book changed.
    bids: list              # [[price, size], ...] best first.
    asks: list              # [[price, size], ...] best first.


@dataclass
class Opportunity:
    """
    A stretch of time when one pair could be traded for a profit after fees.
    """
    polymarket_id: str
    kalshi_id: str
    kind: str
    label: str              # Short human readable name of the bet.
    trade: str              # Which two legs to buy.
    start_ts: str           # When the net edge first went positive.
    end_ts: str             # When it went back to zero, or the last quote seen.
    seconds: float
    peak_ts: str
    peak_edge: float        # Net dollars per contract at the top of book, at the peak.
    peak_size: float        # Contracts fillable at a positive net edge, at the peak.
    peak_profit: float      # Net dollars from filling peak_size, at the peak.
    live: int               # 1 when the game had started, 0 otherwise.
    days_held: float | None     # From the peak until the bet pays out, if held to resolution.
    return_pct: float       # Net edge over the capital tied up, as a percent.
    annual_pct: float | None    # return_pct scaled to a year over days_held, without compounding.
