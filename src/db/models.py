"""
Data classes shared across the project.

Each model lists its key first, as its table does: the row id, or the
columns of the primary key. A row id is keyword only, since it is set when
the row is stored rather than when the model is made.
"""

from dataclasses import dataclass, field


def row_id():
    """
    A model's row id field: keyword only, and None until the row is stored.
    """
    return field(default=None, kw_only=True)


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
    pair_id: int | None = None       # The pair this bet belongs to, set when the pairs are stored.


@dataclass
class Pair:
    """
    The contracts on both venues that describe one bet. The label is the
    bet's identity in words, and the id is what the other tables refer to.
    Members are Bets, two or three of them, since a venue may list both
    sides of a game as contracts.
    """
    id: int | None = row_id()
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
    id: int | None = row_id()
    pair_id: int            # The pair, see pairs.
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
    One trade, paper or live: two legs sent on a scanner signal, what each filled,
    the profit locked in on the matched contracts, and how any mismatch
    was flattened. How its legs paid out is its Settlement, once it has one.
    """
    id: int | None = row_id()
    mode: str = field(kw_only=True)     # 'paper' or 'live', the executor that made the trade.
    pair_id: int            # The pair, see pairs.
    trade: str              # The two legs in words.
    signal_ts: str          # When the scanner signalled.
    edge: float             # Net dollars per contract at the signal.
    quantity: int           # Contracts wanted on each leg.
    cap: int | None = field(default=None, kw_only=True)     # The allocator's cap on contracts per trade when this one was sent.
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
    status: str = "sent"    # 'sent' while in flight, then 'filled', 'partial', or 'failed'.
    label: str | None = None    # The pair's label for log lines, read from the pairs table rather than stored here.
    starts_at: str | None = None    # Kickoff of the game behind the trade, read from the contracts table, for the settler.


@dataclass
class Settlement:
    """
    How a trade's legs paid out once both had resolved. A leg that held
    nothing has no result.
    """
    trade_id: int           # The trade settled, see trades. A trade has at most one settlement.
    mode: str = field(kw_only=True)     # 'paper' or 'live', the trade's mode.
    settled_at: str         # When both legs had resolved and the payouts were booked: the later leg's settlement.
    yes_result: str | None = None       # How the yes leg's contract resolved, 'yes' or 'no'.
    yes_payout: float | None = None     # Dollars received on the yes leg, one per contract held when its side won.
    yes_settled_at: str | None = None   # The venue's settlement time for the yes leg.
    no_result: str | None = None
    no_payout: float | None = None
    no_settled_at: str | None = None


@dataclass
class Order:
    """
    One real order the live executor sent to a venue, and what came back.
    """
    id: int | None = row_id()
    trade_id: int           # The live trade the order was sent for, see trades.
    venue: str
    contract_id: str
    purpose: str            # 'open' for one of the trade's two legs, 'flatten' for an order that evens them.
    action: str             # 'buy' or 'sell'.
    outcome: str            # 'yes' for the contract itself, 'no' for its other side.
    quantity: int           # Contracts asked for.
    limit_price: float      # The worst price per contract accepted for the outcome, before fees.
    client_id: str          # Our id for the order, sent with it, so an order whose answer was lost can be found at the venue.
    sent_at: str
    status: str = "sent"    # 'sent' until the venue answers, then 'filled', 'partial', 'unfilled', 'rejected', or 'error'.
    venue_order_id: str | None = None   # The venue's id for the order, once it answered.
    answered_at: str | None = None
    latency_ms: int | None = None       # From sending the order to its answer.
    filled: int = 0         # Contracts bought or sold.
    dollars: float = 0.0    # Paid for a buy or received for a sale, fees included.
    fees: float = 0.0
    note: str | None = None             # Why the venue rejected the order, or the error.
    response: str | None = None         # The venue's answer as JSON, for reconciling.


@dataclass
class Ledger:
    """
    One cash movement on a venue's paper balance. Live money is read from the venues and has no ledger.
    """
    id: int | None = row_id()
    ts: str
    venue: str
    amount: float           # Dollars in or out, positive when money arrives.
    reason: str             # 'buy', 'sell', 'payout', 'transfer_out', or 'transfer_in', which is also each venue's opening balance.
    trade_id: int | None = None     # The trade behind a buy, sell, or payout.
    balance: float | None = None    # The venue's balance after this entry, so the newest entry gives the balance.


@dataclass
class Transfer:
    """
    A rebalancing transfer between venues.
    """
    id: int | None = row_id()
    from_venue: str
    to_venue: str
    amount: float
    requested_at: str
    expected_at: str        # When the money should land, business days after the request.
    reason: str             # 'drift' for the weekly check, 'floor' for a venue running low.
    arrived_at: str | None = None   # Set when the money was credited to the receiving venue.
