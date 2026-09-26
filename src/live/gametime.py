"""
When a game is played and when the bets on it pay out.

Every timing assumption about games lives here, so the recorder, the
scanner, the executor, and the allocator agree on them. The numbers come
from the first live game, Atlanta at Green Bay: kickoff to final whistle
took 3.05 hours, and both venues settled within half an hour of it.
"""

from common.timeutil import shift

GAME_HOURS = 3.25       # Kickoff to final whistle, with a little margin over the 3.05 measured.
SETTLE_HOURS = 0.5      # Final whistle to the venues settling.
PAYOUT_HOURS = GAME_HOURS + SETTLE_HOURS   # Kickoff to payout, when the money a game holds comes back.
RECORD_HOURS = 5        # Kickoff to when a game's contracts stop being recorded, whatever their close time says.


def in_play(kickoff, now):
    """
    Whether a game that kicked off at kickoff is being played at now.
    """
    return kickoff <= now < shift(kickoff, hours=GAME_HOURS)


def in_play_or_settling(kickoff, now):
    """
    Whether a game that kicked off at kickoff is being played or waiting on the venues to settle at now.
    """
    return kickoff <= now < shift(kickoff, hours=PAYOUT_HOURS)


def resolution_time(start_time, close_time):
    """
    When a contract pays out. Games pay once the venues settle after the final whistle. Futures pay near their close time.
    """
    if start_time:
        return shift(start_time, hours=PAYOUT_HOURS)
    return close_time


def kickoff(members):
    """
    The latest kickoff among the members, or None when none of them is a game.
    """
    return max((m["start_time"] for m in members if m["start_time"]), default=None)


def pays_at(members):
    """
    When the slowest of the members pays out, since capital is locked until then. None when none of them says.
    """
    return max((t for t in (resolution_time(m["start_time"], m["close_time"]) for m in members) if t), default=None)
