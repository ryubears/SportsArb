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
