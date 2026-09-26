"""
Size paper trades so the money covers every game in play.

A game's money is out from its first trade until its contracts settle,
about half an hour after the final whistle. The games in that window at
any moment are the active games, and they share the pool: the free cash
on a venue plus what the active games already hold. Each active game's
share is the pool divided by the number of active games. The cap on one
trade is that share divided by config.DOLLARS_PER_CAP, which turns a dollar
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

from common.venues import VENUES
from db import database
from live.helper import config
from live.helper.game import game_key, in_play, in_play_or_settling


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
        self.kickoffs = database.load_kickoffs(self.conn)

    def active(self, now):
        """
        The games whose money is out right now: kicked off and not yet settled.
        """
        return [key for key, kickoff in self.kickoffs.items() if in_play_or_settling(kickoff, now)]

    def deployed(self):
        """
        Dollars held in open trades per game and venue, as {game key: {venue: dollars}}.
        """
        held = {}
        for key, legs in database.load_open_game_costs(self.conn):
            game = held.setdefault(key, {venue: 0.0 for venue in VENUES})
            for venue, cost in legs:
                game[venue] += cost
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
        cap = int(min(shares.values()) / config.DOLLARS_PER_CAP)
        return 0 if cap < config.MIN_CAP else min(cap, config.MAX_CAP)

    def summary(self, now):
        """
        The active games and the cap a game among them gets, for log lines.
        """
        active, shares = self.shares(now)
        if not active:
            return "capital: no games in play"
        share = min(shares.values())
        return f"capital: {len(active)} games in play or settling, {share:,.0f}$ a venue each, cap {int(share / config.DOLLARS_PER_CAP)}"
