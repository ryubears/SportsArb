"""
Size trades so the money lasts through every game.

A trade's money is out from the moment it is sent until its game's
contracts settle, config.SETTLE_HOURS after the final whistle, and then it
comes back to be spent again. So what one game may spend depends on the
games it overlaps, not on the whole day: the noon games' money pays for
the evening's once they settle.

Every half hour, config.BUDGET_MINUTES, the allocator plans the games in
play and those kicking off within config.PLAN_HOURS. A game is expected to
spend its sport's config.DOLLARS_PER_CAP_HOUR on its busier venue for
every contract of cap, for each hour it is played, and to hold what it
spent until its money is back, SETTLE_HOURS after it ends, which the
scoreboard says. Every game gets a cap, the most contracts one of its
trades may hold. The caps rise together from nothing, and a game's stops
rising at the first moment the money would run out with that game still
holding what it spent, counting what is free on each venue and the money
coming back as games settle. A game that plays into a crowded stretch gets
a smaller cap, and one that is over before the crowd arrives a bigger one.

The half hour's budget on each venue is what the plan expects its games
to spend in it. Every game in play draws on it, first come, first served,
so a busy game takes what a quiet one leaves: on the first full Sunday a
game spent a median of 8 dollars for every contract of cap, but the
quietest spent 2 and the busiest 24. Once the budget is spent, trades wait
for the next half hour, and what is left goes into the next plan with the
rest of the free money. A catalog refresh plans again at once, and the
trades since a plan was made are what count against its budget.

Paper and live trading each plan their own money and trades. A paper cap
under config.MIN_CAP sends nothing. A live cap under config.LIVE_MIN_CAP
is raised to it, since a small live test is thinner than the paper rate
expects, and the half hour's budget still bounds what it spends.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from common.timeutil import seconds_between, shift
from common.venues import VENUES
from db import database
from engine.helper import config
from engine.helper.game import game_key


def cap_range(mode):
    """
    The least and most contracts one trade may hold, kept smaller for live trades than paper ones.
    """
    return (config.LIVE_MIN_CAP, config.LIVE_MAX_CAP) if mode == "live" else (config.MIN_CAP, config.MAX_CAP)


def cash_floor(mode):
    """
    Dollars new trades of the mode leave untouched on each venue, which a plan cannot spend.
    """
    return config.LIVE_CASH_FLOOR if mode == "live" else config.CASH_FLOOR


def half_hour_end(now):
    """
    The end of the budget period now falls in, the next multiple of config.BUDGET_MINUTES after midnight UTC.
    """
    t = datetime.fromisoformat(now)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    period = timedelta(minutes=config.BUDGET_MINUTES)
    return (midnight + ((t - midnight) // period + 1) * period).isoformat()


def hours_played(kickoff, end, start, until):
    """
    Hours of a game played from kickoff to end that fall between start and until.
    """
    return max(0.0, seconds_between(max(kickoff, start), min(end, until)) / 3600)


def raise_together(loads, room):
    """
    Every game's cap, raised together from nothing. loads maps each moment
    to {game: dollars one contract of cap has put in by then and still
    holds}, and room maps each moment to the dollars free by then on the
    venue with the least. When a moment runs out of room, the games holding
    money at it keep the cap reached, and the others rise on.
    """
    caps = {}
    rising = {key for held in loads.values() for key, dollars in held.items() if dollars > 0}
    load = {moment: sum(held.values()) for moment, held in loads.items()}
    used = dict.fromkeys(loads, 0.0)
    while rising:
        levels = [((room[m] - used[m]) / load[m], m) for m in loads if load[m] > 1e-9]
        if not levels:
            break
        level, moment = min(levels)
        stopping = [key for key, dollars in loads[moment].items() if key in rising and dollars > 0]
        if not stopping:
            load[moment] = 0.0          # What was left there was rounding.
            continue
        for key in stopping:
            caps[key] = max(level, 0.0)
            rising.discard(key)
            for m, held in loads.items():
                load[m] -= held.get(key, 0.0)
                used[m] += held.get(key, 0.0) * caps[key]
    return caps


@dataclass
class Plan:
    """
    One half hour's plan: each game's cap, and the budget on each venue that the half hour's trades share.
    """
    made: str           # When it was made. The trades signalled since then count against its budget.
    ends: str           # The end of its half hour, when the next plan is made.
    caps: dict          # Game key maps to contracts of cap, before the mode's bounds.
    budget: float       # Dollars the half hour's trades may put in on each venue.
    playing: list       # The games played in the half hour.
    ahead: int          # Games kicking off after the half hour, within config.PLAN_HOURS.


class Allocator:
    """
    Plans the money of one mode, paper or live, over the games the scoreboard knows.
    """

    def __init__(self, conn, cash, scoreboard):
        self.conn = conn
        self.cash = cash
        self.mode = cash.mode       # Only the trades of this mode hold this money.
        self.scoreboard = scoreboard
        self.plan = None            # The current half hour's Plan, made when first needed.

    def reload(self):
        """
        Plan again when next asked, after the catalog has changed.
        """
        self.plan = None

    def deployed(self):
        """
        Dollars held in open trades per game and venue, as {game key: {venue: dollars}}.
        """
        held = {}
        for key, legs in database.load_open_game_costs(self.conn, self.mode):
            game = held.setdefault(key, {venue: 0.0 for venue in VENUES})
            for venue, cost in legs:
                game[venue] += cost
        return held

    def spendable(self, venue):
        """
        Dollars free for new trades on the venue: its cash less the mode's floor, or nothing while live money is still unread.
        """
        return max(0.0, self.cash[venue] - cash_floor(self.mode)) if self.cash.known(venue) else 0.0

    def make_plan(self, now):
        """
        Plan the half hour now falls in, over the games in play or kicking off within config.PLAN_HOURS.
        """
        board, rate = self.scoreboard, config.DOLLARS_PER_CAP_HOUR
        ends, horizon = half_hour_end(now), shift(now, hours=config.PLAN_HOURS)
        games = {}          # Game key maps to (kickoff, end, when its money is back).
        for key, (kickoff, _) in board.games.items():
            back = board.settles(key, now)
            if kickoff < horizon and back > now:
                games[key] = (kickoff, board.end(key, now), back)
        # A moment is checked just before a game's money comes back, when the most is out at once.
        moments = sorted({back for _, _, back in games.values()})
        loads = {m: {key: rate[key[0]] * hours_played(kickoff, end, now, m) for key, (kickoff, end, back) in games.items() if back >= m}
                 for m in moments}
        held = self.deployed()
        room = {m: min(self.spendable(venue) + sum(dollars[venue] for key, dollars in held.items() if key in games and games[key][2] < m)
                       for venue in VENUES) for m in moments}
        caps = raise_together(loads, room)
        in_half = {key: hours_played(kickoff, end, now, ends) for key, (kickoff, end, _) in games.items()}
        budget = sum(caps.get(key, 0.0) * rate[key[0]] * hours for key, hours in in_half.items())
        return Plan(now, ends, caps, budget, [key for key, hours in in_half.items() if hours > 0],
                    sum(1 for kickoff, _, _ in games.values() if kickoff >= ends))

    def current(self, now):
        """
        The plan for the half hour now falls in, made the first time it is
        asked for. A plan made before the live balances are read is not kept.
        """
        if self.plan is None or now >= self.plan.ends:
            plan = self.make_plan(now)
            if not all(self.cash.known(venue) for venue in VENUES):
                return plan
            self.plan = plan
        return self.plan

    def bounded(self, cap):
        """
        A cap within the mode's range. Under the least, paper sends nothing and live sends the least.
        """
        least, most = cap_range(self.mode)
        if cap < least:
            return least if self.mode == "live" else 0
        return min(int(cap), most)

    def in_play(self, pair, now):
        """
        Whether the pair's game is being played at now, as the scoreboard says.
        """
        key = game_key(pair)
        return key is not None and self.scoreboard.in_play(key, now)

    def cap(self, pair, now):
        """
        Contracts one trade on this pair may hold right now. Zero when its game is not being played.
        """
        if not self.in_play(pair, now):
            return 0
        return self.bounded(self.current(now).caps.get(game_key(pair), 0.0))

    def room(self, now):
        """
        Dollars this half hour's trades may still put in on each venue, as {venue: dollars}.
        """
        plan = self.current(now)
        spent = database.load_spending(self.conn, self.mode, plan.made)
        return {venue: max(0.0, plan.budget - spent.get(venue, 0.0)) for venue in VENUES}

    def summary(self, now):
        """
        The games in the half hour, its budget, and their caps, for log lines.
        """
        plan = self.current(now)
        if not plan.playing:
            return f"{self.mode} capital: no games in play, {plan.ahead} within {config.PLAN_HOURS} hours"
        spent = database.load_spending(self.conn, self.mode, plan.made)
        caps = sorted(self.bounded(plan.caps.get(key, 0.0)) for key in plan.playing)
        return (f"{self.mode} capital: {len(plan.playing)} games in play and {plan.ahead} more within {config.PLAN_HOURS} hours, "
                f"{plan.budget:,.0f}$ a venue this half hour with {max(spent.values(), default=0.0):,.0f}$ put in, caps {caps[0]} to {caps[-1]}")
