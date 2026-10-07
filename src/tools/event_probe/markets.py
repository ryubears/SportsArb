"""
A game's contracts the event probe watches, and what a read of the game's
feed says of each: the bet it stands for already true, already false, or
still open.

A bet is the catalog's, see db.models.Bet: a total or a player's count is
over its line, a spread's team wins by more than its line, a game winner's
team wins, and the contract pays on the bet when its polarity is 'yes',
against it when 'no'. Every count here only grows, so a count past its
line settles the bet at once, unless its play is overturned, and the end
of the game settles the rest. A pitcher taken out of a baseball game
cannot come back, so his pitching bets settle then.
"""

from dataclasses import dataclass
from common import jsonutil

# The counts of a player's bet, by kind, added up, see leagues.MLB_COUNTS and leagues.nhl_snapshot().
COUNTS = {
    "player_hits": ("hits",), "player_home_runs": ("home_runs",), "player_total_bases": ("total_bases",),
    "player_rbis": ("rbis",), "player_hits_runs_rbis": ("hits", "runs", "rbis"), "player_stolen_bases": ("stolen_bases",),
    "player_strikeouts": ("strikeouts",), "player_hits_allowed": ("hits_allowed",), "player_earned_runs_allowed": ("earned_runs",),
    "player_walks_allowed": ("walks_allowed",), "player_outs": ("outs",),
    "player_goals": ("goals",), "player_points": ("goals", "assists"),
}
PITCHING = {"player_strikeouts", "player_hits_allowed", "player_earned_runs_allowed", "player_walks_allowed", "player_outs"}
GAME_KINDS = {"total", "team_total", "spread", "game_winner"}


@dataclass
class Watched:
    """
    One contract on a watched game, and the bet it stands for.
    """
    venue: str
    contract_id: str
    kind: str
    subject: str | None     # The team or player's key the bet is about.
    line: float | None
    polarity: str
    fee_info: dict | None = None


def load_watched(conn, game):
    """
    The game's cataloged contracts whose bets a feed can settle, from the bot's database. A venue may name the teams
    in either order.
    """
    rows = conn.execute("""
        SELECT b.venue, b.contract_id, b.kind, b.subject, b.line, b.polarity, c.fee_info
        FROM bets b JOIN contracts c USING (venue, contract_id)
        WHERE c.sport = ? AND b.game_date = ? AND ((b.team_a = ? AND b.team_b = ?) OR (b.team_a = ? AND b.team_b = ?))
        ORDER BY b.venue, b.contract_id
    """, (game.sport, game.game_date, game.away, game.home, game.home, game.away))
    return [Watched(venue, contract_id, kind, subject, line, polarity, jsonutil.parse(fee_info))
            for venue, contract_id, kind, subject, line, polarity, fee_info in rows if kind in COUNTS or kind in GAME_KINDS]


def count(bet, snap, game):
    """
    The number the bet's line is set against by this read, or None for a
    bet with no line, or about a team or player the read does not have.
    """
    scores = {game.away: snap.away, game.home: snap.home}
    if bet.kind == "total":
        return snap.away + snap.home
    if bet.kind == "team_total":
        return scores.get(bet.subject)
    if bet.kind in COUNTS and bet.subject in snap.players:
        return sum(snap.players[bet.subject].get(name, 0) for name in COUNTS[bet.kind])
    return None


def statement(bet, snap, game):
    """
    Whether the bet is true by this read: True or False once settled, None
    while open, and None for one about a team or player the read does not
    have, which nothing here can settle.
    """
    final = snap.state == "final"
    scores = {game.away: snap.away, game.home: snap.home}
    if bet.kind in ("game_winner", "spread"):
        if not final or bet.subject not in scores:
            return None
        other = snap.home if bet.subject == game.away else snap.away
        return scores[bet.subject] - other > (bet.line or 0)
    value = count(bet, snap, game)
    if value is None:
        return None
    if value > bet.line:
        return True
    if final or (bet.kind in PITCHING and bet.subject in snap.pulled):
        return False
    return None


def winner(bet, truth):
    """
    The outcome of the contract that pays once the bet is settled: 'yes' or 'no', or None while it is open.
    """
    if truth is None:
        return None
    return "yes" if truth == (bet.polarity == "yes") else "no"
