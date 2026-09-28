"""
Decide how much money trades may use: a cap for each game, and a budget
for each half hour.

A game's cap is the most contracts one trade on it may hold. The half
hour's budget is the dollars all trades together may put in on each venue
until the next half hour. A trade asks for config.FILL_SHARE of what the
books show at a good enough price, but never more than its game's cap,
what is left of the budget, or the cash free on each venue, see
executor.py.

Money put into a game is tied up until the game's contracts settle,
config.SETTLE_HOURS after it ends, and then it comes back to be spent
again. So the money does not have to last the whole day, only each
crowded stretch of it: the noon games' money comes back in time for the
evening's. Every config.BUDGET_MINUTES the allocator makes a plan over the
games in play and those kicking off within config.PLAN_HOURS:

1. Each game is expected to spend its sport's config.DOLLARS_PER_CAP_HOUR
   on its busier venue for each contract of its cap, every hour it is
   played. At the NFL's 3.1, a cap of 100 spends about 310 dollars an hour.
2. The most money is tied up just before a game's money comes back, so
   the plan checks each of those moments. What the games still unsettled
   then will spend from now until then has to fit in the cash free now,
   plus what comes back before then from the games that settle earlier,
   on the venue with less.
3. Every game's cap rises from nothing, all together. When a moment runs
   out of money, the games with money tied up at it stop at the cap
   reached, and the others rise on. Games crowded together share the
   money, and a game alone, like a night game, gets most of it.
4. The half hour's budget is what the plan expects its games to spend in
   it at those caps.

Every game in play draws on the budget, first come, first served, so a
busy game takes what a quiet one leaves. Once it is spent, trades wait
for the next half hour, whose plan starts again from what is free then,
so a game's cap can change from one half hour to the next. A catalog
refresh plans again at once.

For example, take a Sunday with 9,500 dollars free on each venue, nine
games kicking off at 17:00 UTC, four at 20:05 and 20:25, and one at
00:20. The first peak is at 20:45, just before the early games' money
comes back. By then each early game has spent a whole game's worth, 10
dollars for each contract of its cap, and each late game 20 or 40
minutes' worth, 1 or 2 dollars: 97 dollars for each contract of cap they
all get. 9,500 pays for 98, so each of the thirteen gets a cap of 98, and
from 17:00 to 17:30 the nine early games share a budget of about 1,370
dollars on each venue. The night game kicks off after the others have
settled, so all 9,500 is its own, a cap of about 940. Once the early
games' money is back, the plans give the late games more.

Paper and live trading each plan their own money and trades. A paper cap
under config.PAPER_MIN_CAP sends nothing. A live cap under
config.LIVE_MIN_CAP is raised to it, since a small live test is thinner
than the paper rate expects, and the budget still bounds what it spends.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from common.timeutil import hours_between, shift
from common.venues import VENUES
from db import database
from engine.helper import config
from engine.helper.game import game_key


def cap_range(mode):
    """
    The least and most contracts one trade may hold, kept smaller for live trades than paper ones.
    """
    return (config.LIVE_MIN_CAP, config.LIVE_MAX_CAP) if mode == "live" else (config.PAPER_MIN_CAP, config.PAPER_MAX_CAP)


def half_hour_end(now):
    """
    The end of the half hour now falls in, the next multiple of config.BUDGET_MINUTES after midnight UTC.
    """
    t = datetime.fromisoformat(now)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    period = timedelta(minutes=config.BUDGET_MINUTES)
    return (midnight + ((t - midnight) // period + 1) * period).isoformat()


def hours_played(kickoff, end, start, until):
    """
    Hours of a game played from kickoff to end that fall between start and until.
    """
    return max(0.0, hours_between(max(kickoff, start), min(end, until)))


def raise_together(tied, free):
    """
    Every game's cap, raised together from nothing, as {game: cap}. tied
    maps each moment checked to {game: dollars one contract of the game's
    cap has tied up at that moment}, and free maps each moment to the
    dollars that can be tied up at it. When the rising caps would tie up
    more than a moment has, the games tied up at it stop at the cap reached,
    and the others rise on until a moment stops them too.
    """
    caps = {}
    rising = {game for games in tied.values() for game, dollars in games.items() if dollars > 0}
    per_cap = {m: sum(games.values()) for m, games in tied.items()}     # Dollars a moment ties up for each contract the rising caps add.
    taken = dict.fromkeys(tied, 0.0)                                      # Dollars a moment has tied up for the games already stopped.
    while rising:
        limits = [((free[m] - taken[m]) / per_cap[m], m) for m in tied if per_cap[m] > 1e-9]
        if not limits:
            break
        cap, moment = min(limits)           # The first moment to run out, and the cap at which it does.
        stopping = [game for game, dollars in tied[moment].items() if game in rising and dollars > 0]
        if not stopping:
            per_cap[moment] = 0.0           # What was left there was rounding.
            continue
        for game in stopping:
            caps[game] = max(cap, 0.0)
            rising.discard(game)
            for m, games in tied.items():
                per_cap[m] -= games.get(game, 0.0)
                taken[m] += games.get(game, 0.0) * caps[game]
    return caps


@dataclass
class Plan:
    """
    One half hour's plan: every game's cap, and the budget on each venue that the half hour's trades share.
    """
    made: str           # When it was made. The trades signalled since then count against its budget.
    ends: str           # The end of its half hour, when the next plan is made.
    caps: dict          # Game key maps to its cap, in contracts, before the mode's bounds.
    budget: float       # Dollars the half hour's trades may put in on each venue, all games together.
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
        for key, venue, cost in database.load_open_legs(self.conn, self.mode):
            if key:
                held.setdefault(key, {v: 0.0 for v in VENUES})[venue] += cost
        return held

    def games_ahead(self, now):
        """
        The games a plan made at now covers, as {game key: (kickoff, end,
        when its money is back)}: those kicking off within config.PLAN_HOURS
        whose money is not back yet. The scoreboard says when each ends.
        """
        board, horizon = self.scoreboard, shift(now, hours=config.PLAN_HOURS)
        games = {}
        for key, (kickoff, _) in board.games.items():
            back = board.settles(key, now)
            if kickoff < horizon and back > now:
                games[key] = (kickoff, board.end(key, now), back)
        return games

    def make_plan(self, now):
        """
        Plan the half hour now falls in, in the steps the module's docstring gives.
        """
        rate, ends = config.DOLLARS_PER_CAP_HOUR, half_hour_end(now)
        games = self.games_ahead(now)
        # Steps 1 and 2. The moments checked, each just before a game's money comes back. At each, one contract of a game's
        # cap has tied up what the game spends from now until then, while its money is still out.
        moments = sorted({back for _, _, back in games.values()})
        tied = {m: {key: rate[key[0]] * hours_played(kickoff, end, now, m) for key, (kickoff, end, back) in games.items() if back >= m}
                for m in moments}
        # What can be tied up at each moment: the cash free now, plus what open trades hold on the games whose money is
        # back before then, on the venue with less.
        held = self.deployed()
        free = {m: min(self.cash.spendable(venue) + sum(dollars[venue] for key, dollars in held.items() if key in games and games[key][2] < m)
                       for venue in VENUES) for m in moments}
        # Step 3.
        caps = raise_together(tied, free)
        # Step 4. What the games are expected to spend in the half hour at those caps.
        hours = {key: hours_played(kickoff, end, now, ends) for key, (kickoff, end, _) in games.items()}
        budget = sum(caps.get(key, 0.0) * rate[key[0]] * played for key, played in hours.items())
        return Plan(now, ends, caps, budget, [key for key, played in hours.items() if played > 0],
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
        The most contracts one trade on this pair may hold right now. Zero when its game is not being played.
        """
        if not self.in_play(pair, now):
            return 0
        return self.bounded(self.current(now).caps.get(game_key(pair), 0.0))

    def budget_left(self, now):
        """
        What is left of the half hour's budget on each venue, as {venue: dollars}, after the trades since the plan was made.
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
                f"caps {caps[0]} to {caps[-1]}, budget {plan.budget:,.0f}$ a venue this half hour with {max(spent.values(), default=0.0):,.0f}$ used")
