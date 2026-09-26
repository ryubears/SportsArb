"""
Size paper trades so the money covers every game in play.

A game's money is out from its first trade until its contracts settle,
about half an hour after the final whistle. The games in that window at
any moment are the active games, and they share the pool: the free cash
on a venue plus what the active games already hold. Each active game's
share is the pool divided by the number of active games. The cap on one
trade is that share divided by DOLLARS_PER_CAP, which turns a dollar
budget into a cap: on the first live game, every contract of cap led to
about 20 dollars of spending on each venue by the final whistle, so a
budget of 1,000 dollars is spent by a cap of 50. Both venues are sized
and the smaller cap wins.

The cap only moves when the active set changes. It holds through a game
while the same games are in play, then grows for the games still running
when others settle, since their money comes back to the pool and fewer
games share it. A game that has spent its share gets nothing more, and a
game not in play gets nothing at all, since trades are only taken live.
"""

from common.timeutil import shift
from common.venues import VENUES

GAME_HOURS = 3.25       # Kickoff to final whistle, with a little margin over the 3.05 measured.
SETTLE_HOURS = 0.5      # Final whistle to the venues settling, from the first live game.
DOLLARS_PER_CAP = 20    # Dollars a game spends on each venue, over the whole game, for every contract of cap. From the first live game.
MIN_CAP = 5             # Contracts per trade, the least worth sending.
MAX_CAP = 500           # Contracts per trade, the most one trade may hold.


def game_key(pair):
    """
    What identifies a game across its pairs, or None for a bet with no game.
    """
    return (pair["game_date"], pair["team_a"], pair["team_b"]) if pair.get("game_date") else None


def in_play(kickoff, now):
    """
    Whether a game that kicked off at kickoff is being played at now.
    """
    return kickoff <= now < shift(kickoff, hours=GAME_HOURS)


class Allocator:
    """
    The cap on one trade for a pair, from the games sharing the pool right now.
    """

    def __init__(self, conn, cash):
        self.conn = conn
        self.cash = cash
        self.kickoffs = {}      # game key maps to kickoff.
        self.reload()

    def reload(self):
        """
        Load the kickoff of every game the catalog pairs.
        """
        self.kickoffs = {(d, a, b): kickoff for d, a, b, kickoff in self.conn.execute("""
            SELECT p.game_date, p.team_a, p.team_b, MAX(c.start_time)
            FROM pairs p JOIN bets b ON b.pair_id = p.id JOIN contracts c ON c.venue = b.venue AND c.contract_id = b.contract_id
            WHERE p.game_date IS NOT NULL GROUP BY 1, 2, 3 HAVING MAX(c.start_time) IS NOT NULL""")}

    def active(self, now):
        """
        The games whose money is out right now: kicked off and not yet settled.
        """
        return [key for key, kickoff in self.kickoffs.items() if kickoff <= now < shift(kickoff, hours=GAME_HOURS + SETTLE_HOURS)]

    def deployed(self):
        """
        Dollars held in open trades per game and venue, as {game key: {venue: dollars}}.
        """
        held = {}
        for d, a, b, yv, yc, nv, nc in self.conn.execute("""
            SELECT p.game_date, p.team_a, p.team_b, t.yes_venue, t.yes_cost, t.no_venue, t.no_cost
            FROM trades t JOIN pairs p ON p.id = t.pair_id
            WHERE t.settled_at IS NULL AND t.yes_held + t.no_held > 0 AND p.game_date IS NOT NULL"""):
            game = held.setdefault((d, a, b), {venue: 0.0 for venue in VENUES})
            game[yv] += yc
            game[nv] += nc
        return held

    def shares(self, now):
        """
        The active games and each venue's share of its pool per game, as (games, {venue: dollars}).
        """
        active = self.active(now)
        if not active:
            return active, {}
        held = self.deployed()
        return active, {venue: (self.cash[venue] + sum(held.get(key, {}).get(venue, 0.0) for key in active)) / len(active) for venue in VENUES}

    def cap(self, pair, now):
        """
        Contracts one trade on this pair may hold right now. Zero when the
        game is not being played or has spent its share of the pool.
        """
        key = game_key(pair)
        if key not in self.kickoffs or not in_play(self.kickoffs[key], now):
            return 0
        active, shares = self.shares(now)
        held = self.deployed().get(key, {})
        if any(shares[venue] - held.get(venue, 0.0) <= 0 for venue in VENUES):
            return 0
        cap = int(min(shares.values()) / DOLLARS_PER_CAP)
        return 0 if cap < MIN_CAP else min(cap, MAX_CAP)

    def summary(self, now):
        """
        The active games and the cap a game among them gets, for log lines.
        """
        active, shares = self.shares(now)
        if not active:
            return "capital: no games in play"
        share = min(shares.values())
        return f"capital: {len(active)} games in play or settling, {share:,.0f}$ a venue each, cap {int(share / DOLLARS_PER_CAP)}"
