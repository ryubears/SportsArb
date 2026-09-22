"""
Data classes shared across the project.
"""

from dataclasses import dataclass


@dataclass
class Contract:
    """
    One tradable Yes side outcome, described the same way for every venue.
    """
    venue: str
    contract_id: str        # Kalshi market ticker, or Polymarket US market slug.
    market_id: str          # Kalshi market ticker, or Polymarket US market id.
    event_id: str           # Kalshi event ticker, or Polymarket US event slug.
    series_id: str | None   # Kalshi series ticker, or Polymarket US series slug.
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
    pair_label: str | None = None    # The pair this bet belongs to, set by match.py.


@dataclass
class Pair:
    """
    The contracts on both venues that describe one bet. The label is the
    bet's identity in words and serves as its key. Members are Bets, two or
    three of them, since a venue may list both sides of a game as contracts.
    """
    label: str              # For example 'spread 2026-09-20 CAR@ATL ATL 4.5'.
    kind: str
    season: int | None
    game_date: str | None
    team_a: str | None
    team_b: str | None
    subject: str | None
    line: float | None
    members: list           # Bets, one per contract.
    flags: list[str]        # Things a human should check before trusting the pair.

    @property
    def venues(self):
        return sorted({m.venue for m in self.members})


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
    A stretch of time when one pair could be traded for a profit after
    fees, by buying yes exposure on one contract and no exposure on another.
    """
    label: str              # The pair's label.
    kind: str
    trade: str              # The two legs in words.
    yes_venue: str          # Where the yes exposure was cheapest at the peak.
    yes_contract: str
    no_venue: str           # Where the no exposure was cheapest at the peak.
    no_contract: str
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


@dataclass
class Gap:
    """
    A stretch when a venue's feed was down, so its books could not be trusted.
    """
    venue: str
    start_ts: str           # When the connection was lost.
    end_ts: str | None      # When a new connection was subscribed. None while still down.


@dataclass
class Trade:
    """
    One paper trade: two legs sent on a scanner signal, what each filled,
    the profit locked in on the matched contracts, and how any mismatch
    was flattened.
    """
    label: str              # The pair's label.
    kind: str
    trade: str              # The two legs in words.
    signal_ts: str          # When the scanner signalled.
    edge: float             # Net dollars per contract at the signal.
    quantity: int           # Contracts wanted on each leg.
    yes_venue: str
    yes_contract: str
    yes_polarity: str       # The side the contract pays on, so settlement knows whether the leg won.
    yes_limit: float        # The cost per contract seen at the signal, used as the limit.
    no_venue: str
    no_contract: str
    no_polarity: str
    no_limit: float
    pays_at: str            # When the slower leg pays out.
    yes_filled: int = 0
    yes_cost: float = 0.0   # Dollars paid including fees.
    yes_latency_ms: int = 0
    yes_fill_ts: str | None = None
    no_filled: int = 0
    no_cost: float = 0.0
    no_latency_ms: int = 0
    no_fill_ts: str | None = None
    yes_held: int = 0       # Contracts still held on the yes leg after any flattening.
    no_held: int = 0
    matched: int = 0        # Contracts held on both sides, after any flattening.
    profit: float = 0.0     # Dollars locked in on the matched contracts, after fees.
    hedge: str = "none"     # How the mismatch was flattened, in words.
    hedge_pnl: float = 0.0  # Dollars gained or lost by flattening, after fees.
    status: str = "sent"    # 'filled', 'partial', or 'failed'.
    settled_at: str | None = None   # When both legs had resolved and the payouts were booked.
    id: int | None = None   # The row id once stored.


@dataclass
class Settlement:
    """
    What one leg of a trade paid out when its contract resolved.
    """
    trade_id: int
    venue: str
    contract_id: str
    side: str               # 'yes' or 'no', the side of the bet this leg held.
    held: int               # Contracts held at resolution.
    cost: float             # Dollars paid for them including fees.
    result: str             # How the contract resolved, 'yes' or 'no'.
    payout: float           # Dollars received, one per contract when the held side won.
    realized: float         # payout minus cost.
    settled_at: str         # The venue's settlement time.


@dataclass
class Ledger:
    """
    One cash movement on a venue's paper balance.
    """
    ts: str
    venue: str
    amount: float           # Dollars in or out, positive when money arrives.
    reason: str             # 'buy', 'sell', 'payout', 'transfer_out', or 'transfer_in'.
    trade_id: int | None = None     # The trade behind a buy, sell, or payout.


@dataclass
class Transfer:
    """
    A rebalancing transfer between venues.
    """
    from_venue: str
    to_venue: str
    amount: float
    requested_at: str
    expected_at: str        # When the money should land, business days after the request.
    reason: str             # 'drift' for the weekly check, 'floor' for a venue running low.
    arrived_at: str | None = None   # Set when the money was credited to the receiving venue.
    id: int | None = None   # The row id once stored.
