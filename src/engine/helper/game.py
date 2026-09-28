"""
Which game a bet is on, when the game is played, and when the bets on it pay out.

Every timing assumption about games lives here, so the recorder, the
scanner, the executor, and the allocator agree on them. How long a game
lasts depends on the sport, see config.GAME_HOURS, which is set where
about three quarters of the sport's past games had ended. Both venues
settled the first live game, Atlanta at Green Bay, within half an hour of
its final whistle.
"""

from common.timeutil import shift
from engine.helper import config


def game_key(pair):
    """
    What identifies a game across its pairs, or None for a bet with no game.
    The sport is part of it, since team codes repeat across leagues.
    """
    return (pair["sport"], pair["game_date"], pair["team_a"], pair["team_b"]) if pair.get("game_date") else None


def payout_hours(sport):
    """
    Kickoff to payout in the sport, when the money a game holds comes back: the game, then the venues settling.
    """
    return config.GAME_HOURS[sport] + config.SETTLE_HOURS


def in_play(kickoff, now, sport):
    """
    Whether a game of the sport that kicked off at kickoff is being played at now.
    """
    return kickoff <= now < shift(kickoff, hours=config.GAME_HOURS[sport])


def in_play_or_settling(kickoff, now, sport):
    """
    Whether a game of the sport that kicked off at kickoff is being played or waiting on the venues to settle at now.
    """
    return kickoff <= now < shift(kickoff, hours=payout_hours(sport))


def resolution_time(start_time, close_time, sport):
    """
    When a contract of the sport pays out. Games pay once the venues settle after the final whistle. Futures pay near their close time.
    """
    if start_time:
        return shift(start_time, hours=payout_hours(sport))
    return close_time


def kickoff(members):
    """
    The latest kickoff among the members, or None when none of them is a game.
    """
    return max((m["start_time"] for m in members if m["start_time"]), default=None)


def pays_at(members, sport):
    """
    When the slowest of a pair's members pays out, since capital is locked until then. None when none of them says.
    """
    return max((t for t in (resolution_time(m["start_time"], m["close_time"], sport) for m in members) if t), default=None)
