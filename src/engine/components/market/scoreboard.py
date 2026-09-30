"""
Know which games are being played, and when each one really ends.

The books do not say when a game is over, so the rest of the engine asks
the scoreboard. It holds every paired game's kickoff and the Polymarket
US event that reports how the game stands, and every
config.SCOREBOARD_SECONDS it asks that venue about the games under way:
live, or over and when. A game is in play from its kickoff until the
venue says it has ended. Past its expected length, config.GAME_HOURS, it
stays in play only while the venue keeps saying it is live, so a game the
venue gives no state for, or a stretch when the venue cannot be reached,
ends at the expected length. Once a game is over the executors leave its
pairs alone, since its books may linger while the venues settle. The
lookup runs in a thread, off the session's tick, the way the settler's
does.
"""

import asyncio
from api import polymarket_us
from common.log import with_traceback
from common.periodic import Periodic
from common.timeutil import hours_between, seconds_between
from db import database
from engine.helper import config
from engine.helper.game import game_label, in_play, recorded_since

LIVE_CHECKS = 3     # Lookups a live reading holds for, so one that fails does not end a game early.


class Scoreboard:
    """
    The paired games of the sports and how each one stands. states is how
    the venue reports on events, polymarket_us.game_states unless a test
    gives its own.
    """

    def __init__(self, conn, sports=None, log=print, states=polymarket_us.game_states):
        self.conn = conn
        self.sports = sports
        self.log = log
        self.states = states
        self.games = {}             # Game key maps to (kickoff, the event its state is read from).
        self.ended = {}             # Game key maps to when the venue says the game ended.
        self.live_at = {}           # Game key maps to when the venue last said the game was live.
        self.lookups = Periodic(lambda: config.SCOREBOARD_SECONDS, log, "scoreboard check")
        self.reload()

    def reload(self):
        """
        Read the paired games again, after a catalog refresh, and forget the
        games no longer paired, whose markets have closed.
        """
        self.games = database.load_games(self.conn, self.sports)
        self.ended = {key: t for key, t in self.ended.items() if key in self.games}
        self.live_at = {key: t for key, t in self.live_at.items() if key in self.games}

    def in_play(self, key, now):
        """
        Whether the game is being played at now: kicked off, not ended, and
        within its expected length or live at the venue's latest word.
        """
        if key not in self.games or now < self.games[key][0]:
            return False
        if key in self.ended:
            return now < self.ended[key]
        if in_play(self.games[key][0], now, key[0]):
            return True
        live = self.live_at.get(key)
        return live is not None and seconds_between(live, now) <= LIVE_CHECKS * config.SCOREBOARD_SECONDS

    def over(self, key, now):
        """
        Whether the game is over at now: kicked off and no longer in play. A game the catalog does not pair is not known to be.
        """
        return key in self.games and self.games[key][0] <= now and not self.in_play(key, now)

    def under_way(self, now):
        """
        The games to ask about, as {game key: event}: kicked off less than
        config.RECORD_HOURS ago, when their books stop being recorded, and
        not yet ended.
        """
        return {key: event for key, (kickoff, event) in self.games.items()
                if event and key not in self.ended and recorded_since(now) < kickoff <= now}

    async def check(self, now):
        """
        Ask the venue how the games under way stand, and note which are live and which have ended.
        """
        wanted = self.under_way(now)
        if not wanted:
            return
        try:
            states = await asyncio.to_thread(self.states, list(wanted.values()))
        except Exception as e:
            self.log(with_traceback(f"scoreboard lookup failed ({e!r}), will retry", e))
            return
        for key, event in wanted.items():
            state = states.get(event)
            if not state:
                continue
            if state["ended"]:
                self.ended[key] = min(state["finished"] or now, now)
                self.live_at.pop(key, None)
                self.log(f"game over: {game_label(key)}, {hours_between(self.games[key][0], self.ended[key]):.2f} hours after kickoff")
            elif state["live"]:
                self.live_at[key] = now

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a lookup when one is due and none is running.
        """
        self.lookups.tick(clock, lambda: self.check(now))
