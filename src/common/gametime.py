"""
Which game a bet is on, when the game is played, and when the bets on it pay out.

Every timing assumption about games lives here, so the recorder, the
scanner, the executor, and the allocator agree on them. The numbers come
from the first live game, Atlanta at Green Bay: kickoff to final whistle
took 3.05 hours, and both venues settled within half an hour of it.
"""

from common import config
from common.timeutil import shift


def game_key(pair):
    """
    What identifies a game across its pairs, or None for a bet with no game.
    """
    return (pair["game_date"], pair["team_a"], pair["team_b"]) if pair.get("game_date") else None


def payout_hours():
    """
    Kickoff to payout, when the money a game holds comes back: the game, then the venues settling.
    """
    return config.GAME_HOURS + config.SETTLE_HOURS


def in_play(kickoff, now):
    """
    Whether a game that kicked off at kickoff is being played at now.
    """
    return kickoff <= now < shift(kickoff, hours=config.GAME_HOURS)


def in_play_or_settling(kickoff, now):
    """
    Whether a game that kicked off at kickoff is being played or waiting on the venues to settle at now.
    """
    return kickoff <= now < shift(kickoff, hours=payout_hours())


def resolution_time(start_time, close_time):
    """
    When a contract pays out. Games pay once the venues settle after the final whistle. Futures pay near their close time.
    """
    if start_time:
        return shift(start_time, hours=payout_hours())
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
