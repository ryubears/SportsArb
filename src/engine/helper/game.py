"""
Which game a bet is on, when the game is played, and when the bets on it pay out.

Every timing assumption about games lives here, so the recorder, the
scanner, and the executor agree on them: when a game is expected to end,
when its money comes back, how long after kickoff it may still be under
way, and when its books go stale if they stop changing. How long a game
is expected to last depends on the sport, see config.GAME_HOURS: three in
four of the sport's past games had ended by then. Both venues settled the
first live game, Atlanta at Green Bay, within half an hour of its final
whistle.
"""

from common.timeutil import days_between, shift
from engine.helper import config


def game_key(pair):
    """
    What identifies a game across its pairs, or None for a bet with no game.
    The sport is part of it, since team codes repeat across leagues.
    """
    return (pair["sport"], pair["game_date"], pair["team_a"], pair["team_b"]) if pair.get("game_date") else None


def game_label(key):
    """
    A game key in words, for log lines, for example 'nfl 2026-09-20 CAR@ATL'.
    """
    sport, game_date, away, home = key
    return f"{sport} {game_date} {away}@{home}"


def expected_end(kickoff, sport):
    """
    When a game of the sport that kicked off at kickoff is expected to end, config.GAME_HOURS later.
    """
    return shift(kickoff, hours=config.GAME_HOURS[sport])


def money_back(end):
    """
    When the money on a game that ends at end comes back: config.SETTLE_HOURS later, once the venues have settled.
    """
    return shift(end, hours=config.SETTLE_HOURS)


def recorded_since(now):
    """
    The kickoff after which a game may still be under way at now, config.RECORD_HOURS earlier. Its books are recorded
    until then, whatever its contracts' close times say.
    """
    return shift(now, hours=-config.RECORD_HOURS)


def resolution_time(start_time, close_time, sport):
    """
    When a contract of the sport pays out: once the venues settle after the final whistle, or, for a contract whose
    venue gives no kickoff, like Kalshi's, at its close time. A pair pays at the latest of its members', so the kickoff rules.
    """
    if start_time:
        return money_back(expected_end(start_time, sport))
    return close_time


def kickoff(members):
    """
    The latest kickoff among the members, or None when none of them is a game.
    """
    return max((m["start_time"] for m in members if m["start_time"]), default=None)


def books_age(game_date, members, now):
    """
    Whether the books of a pair on the game of game_date, None for a future,
    with these members go stale when they stop changing, see pricing.fresh():
    once its game may have started, or at once when no member gives the
    kickoff. Before kickoff a game's markets are open, and a future's until
    its season settles, so their books may rest unchanged for hours.
    """
    if game_date is None:
        return False
    start = kickoff(members)
    return start is None or start <= now


def days_until(now, pays_at):
    """
    Days from now until a bet paying at pays_at pays, at least an hour, for its return a year.
    """
    return max(days_between(now, pays_at), 1 / 24)


def pays_at(members, sport, now=None):
    """
    When the slowest of a pair's members pays out, since capital is locked until then. None when none of them says.
    Given now, a trade's time, it is no sooner than config.SETTLE_HOURS after it: a game still being traded has not
    ended, even one that runs past its expected length.
    """
    times = [t for t in (resolution_time(m["start_time"], m["close_time"], sport) for m in members) if t]
    if times and now:
        times.append(money_back(now))
    return max(times, default=None)
