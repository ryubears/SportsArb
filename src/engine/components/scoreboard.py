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
ends at the expected length. A game's money is expected back
config.SETTLE_HOURS after its end, the real one once the venue has given
it, which is what the allocator plans with. The lookup runs in a thread,
off the session's tick, the way the settler's does.
"""

import asyncio
from api import polymarket_us
from common.log import on_failure, with_traceback
from common.timeutil import seconds_between, shift
from db import database
from engine.helper import config
from engine.helper.game import in_play

LIVE_CHECKS = 3     # Lookups a live reading holds for, so one that fails does not end a game early.


def label(key):
    """
    A game key in words, for log lines, for example 'nfl 2026-09-20 CAR@ATL'.
    """
    sport, game_date, away, home = key
    return f"{sport} {game_date} {away}@{home}"


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
        self.running = None         # The lookup while one runs.
        self.last_check = 0.0       # Wall clock seconds of the last lookup.
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

    def end(self, key, now):
        """
        When the game ends: when the venue says it did, or else when it is
        expected to, or now for a game still live past that.
        """
        if key in self.ended:
            return self.ended[key]
        expected = shift(self.games[key][0], hours=config.GAME_HOURS[key[0]])
        return max(expected, now) if self.in_play(key, now) else expected

    def settles(self, key, now):
        """
        When the game's money is expected back, config.SETTLE_HOURS after it ends.
        """
        return shift(self.end(key, now), hours=config.SETTLE_HOURS)

    def under_way(self, now):
        """
        The games to ask about, as {game key: event}: kicked off less than
        config.RECORD_HOURS ago, when their books stop being recorded, and
        not yet ended.
        """
        return {key: event for key, (kickoff, event) in self.games.items()
                if event and key not in self.ended and kickoff <= now < shift(kickoff, hours=config.RECORD_HOURS)}

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
                self.log(f"game over: {label(key)}, {seconds_between(self.games[key][0], self.ended[key]) / 3600:.2f} hours after kickoff")
            elif state["live"]:
                self.live_at[key] = now

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a lookup when one is due and none is running.
        """
        if clock - self.last_check >= config.SCOREBOARD_SECONDS and (self.running is None or self.running.done()):
            self.last_check = clock
            self.running = asyncio.create_task(self.check(now))
            self.running.add_done_callback(on_failure(self.log, "scoreboard check"))
