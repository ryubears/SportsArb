"""
Data classes shared across the project.
"""

from dataclasses import dataclass, field


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
    raw: dict = field(repr=False)


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
