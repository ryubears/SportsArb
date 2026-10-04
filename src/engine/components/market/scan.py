"""
Find arbitrage episodes in pairs as the recorder's books change.

Whenever a member's book changes, the scanner finds the cheapest way to
hold yes and the cheapest way to hold no across the pair's members, on
any venues, and prices buying both. An episode is a stretch where that
net edge stays above zero after fees. Each episode becomes an Opportunity
with its two legs, its duration, its peak edge, how many contracts could
have been filled at the peak by walking the books' depth, and the return
on the capital tied up, annualized as if held until the bet pays out.

Within an episode the edge worth trading, config.MIN_EDGE or more, may
come and go. The Opportunity also keeps the longest unbroken stretch of it,
and the contracts that stayed fillable at that edge through all of it,
which is what an order sent any time in the stretch could have had, and
so what an edge that lasts is worth.

The recorder drives the Scanner with the books it holds in memory and
the Scanner stores every episode as it ends, so the opportunities table
is the log of everything it saw. The summary script reads it. The
pricing itself lives in pricing.py.

A desk that turns an edge down only to wait for a book to catch up asks
for the pair again once the wait ends, through recheck(), so the edge is
offered then rather than at the next change or tick, up to a second later.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from common.timeutil import now_iso, seconds_between
from db import database
from db.models import Opportunity
from engine.helper import config
from engine.helper.game import days_until, pays_at as payout_time, started
from engine.helper.pricing import Priced, annual_pct, best_trade, fresh, return_pct, trade_words

RECHECK_MARGIN = 0.005      # Seconds past a wait's end that a recheck prices the pair, so the wait is surely over by our clock.


@dataclass
class Episode:
    """
    One pair's stretch of positive net edge, while the scanner follows it.
    """
    pair: dict              # The pair, as the scanner loaded it.
    start_ts: str           # When the edge went positive.
    peak: Priced            # The best moment so far.
    peak_ts: str            # When the best moment was.
    taken: set = field(default_factory=set)     # Which of the scanner's on_signals took a trade on it, by position.
    worth_since: str | None = None      # When the current stretch at config.MIN_EDGE or more began, or None outside one.
    worth_least: tuple = (0.0, 0.0)     # The fewest contracts fillable at that edge so far in the stretch, and their profit.
    worth_best: tuple | None = None     # The longest stretch so far, as (seconds, contracts, profit), a moment's included.

    def end_stretch(self, now):
        """
        Close the current stretch at config.MIN_EDGE or more at now, keeping it when it is the longest.
        """
        if self.worth_since is None:
            return
        seconds = seconds_between(self.worth_since, now)
        if self.worth_best is None or seconds > self.worth_best[0]:
            self.worth_best = (seconds, *self.worth_least)
        self.worth_since = None

    def see(self, priced, now):
        """
        Follow the stretches at config.MIN_EDGE or more through one pricing of the pair.
        """
        if priced is not None and priced.edge >= config.MIN_EDGE:
            least = (priced.min_edge_size, priced.min_edge_profit)
            if self.worth_since is None:
                self.worth_since, self.worth_least = now, least
            elif least[0] < self.worth_least[0]:
                self.worth_least = least
        else:
            self.end_stretch(now)

    def opportunity(self, end_ts):
        """
        The episode as an Opportunity, ended at end_ts.
        """
        self.end_stretch(end_ts)
        worth_seconds, worth_size, worth_profit = self.worth_best or (0.0, 0.0, 0.0)
        start_time = next((m["start_time"] for m in self.pair["members"] if m["start_time"]), None)
        live = 1 if start_time and self.peak_ts >= start_time else 0
        # Capital is locked until the slower of the two legs pays, so the later resolution counts.
        pays_at = payout_time((self.peak.yes, self.peak.no), self.pair["sport"])
        days_held = days_until(self.peak_ts, pays_at) if pays_at else None
        return Opportunity(
            pair_id=self.pair["id"],
            trade=trade_words(self.peak.yes, self.peak.no),
            yes_venue=self.peak.yes["venue"],
            yes_contract=self.peak.yes["contract_id"],
            no_venue=self.peak.no["venue"],
            no_contract=self.peak.no["contract_id"],
            start_ts=self.start_ts,
            end_ts=end_ts,
            seconds=seconds_between(self.start_ts, end_ts),
            peak_ts=self.peak_ts,
            peak_edge=self.peak.edge,
            peak_size=self.peak.size,
            peak_profit=self.peak.profit,
            live=live,
            days_held=days_held,
            return_pct=return_pct(self.peak.edge),
            annual_pct=annual_pct(self.peak.edge, days_held) if days_held else None,
            min_edge_seconds=worth_seconds,
            min_edge_size=worth_size,
            min_edge_profit=worth_profit,
        )


class Scanner:
    """
    Tracks episodes for the pairs of the sports from a map of the newest
    books, keyed by (venue, contract_id). Only the pairs a contract belongs
    to are priced when its book changes. A member is left out while its book
    is older than config.MAX_BOOK_AGE or missing from the map, which is how the
    recorder says a venue's books went unseen. Pairs and fee schedules come
    from the database and are reloaded after each catalog refresh. Every
    finished episode is stored at once, and the big ones are logged. Each
    of on_signals, one per executor, is offered every moment of an episode
    until it takes a trade, and then not again, see trading/executor.py.
    """

    def __init__(self, conn, sports, log=print, on_signals=(), books=None):
        self.conn = conn
        self.sports = sports
        self.log = log
        self.on_signals = list(on_signals)  # Each is called with the trade to make until it takes one in the episode.
        self.books = books                  # Returns the recorder's books, for a recheck, which no book change brings.
        self.rechecks = {}                  # Pair id maps to the timer that prices it again once a desk's wait ends.
        self.episodes = {}                  # Pair id maps to its open Episode.
        self.finished = []                  # (kind, Opportunity) for episodes ended since the last summary.
        self.reload()

    def close(self, pair_id, now):
        """
        End an episode, store it, and log it when it was worth something.
        """
        episode = self.episodes.pop(pair_id)
        timer = self.rechecks.pop(pair_id, None)
        if timer is not None:
            timer.cancel()
        o = episode.opportunity(now)
        database.insert_opportunities(self.conn, [o])
        self.finished.append((episode.pair["kind"], o))
        if o.peak_profit >= config.LOG_PROFIT_DOLLARS:
            self.log(f"episode {episode.pair['label']}: {o.trade}, {100 * o.peak_edge:.1f}c x {o.peak_size:.0f} = {o.peak_profit:.2f}$, lasted {o.seconds:.1f}s, "
                     f"{100 * config.MIN_EDGE:.0f}c or more for {o.min_edge_seconds:.1f}s with {o.min_edge_size:.0f} contracts throughout")

    def reload(self):
        """
        Load the pairs and fee schedules again, ending open episodes of pairs that are gone.
        """
        self.fee_infos, self.pairs = defaultdict(dict), {}
        for sport in self.sports:
            self.fee_infos.update(database.load_fee_infos(self.conn, sport))
            self.pairs.update(database.load_pairs(self.conn, sport))
        by_contract = defaultdict(list)
        for pair_id, pair in self.pairs.items():
            for m in pair["members"]:
                by_contract[(m["venue"], m["contract_id"])].append(pair_id)
        self.by_contract = dict(by_contract)
        for pair_id in [pair_id for pair_id in self.episodes if pair_id not in self.pairs]:
            self.close(pair_id, now_iso())

    def price(self, pair, books, now):
        """
        The best trade across the pair's members whose books are fresh, as a Priced, or None. Books age only
        once their game may have started, see game.started().
        """
        aging = started(pair["game_date"], pair["members"], now)
        members = [m for m in pair["members"] if fresh(books.get((m["venue"], m["contract_id"])), now, aging)]
        if len(members) < 2:
            return None
        return best_trade(members, books, self.fee_infos)

    def update(self, pair_id, books, now):
        """
        Open, extend, or end the episode for one pair from the current books.
        """
        pair = self.pairs[pair_id]
        priced = self.price(pair, books, now)
        episode = self.episodes.get(pair_id)
        if priced is not None and priced.edge > 0:
            if episode is None:
                episode = self.episodes[pair_id] = Episode(pair, now, priced, now)
            elif priced.edge > episode.peak.edge:
                episode.peak, episode.peak_ts = priced, now
            episode.see(priced, now)
            for i, on_signal in enumerate(self.on_signals):
                if i not in episode.taken and on_signal(pair, priced.yes, priced.no, priced.edge, priced.size, self.fee_infos, now):
                    episode.taken.add(i)
        elif episode is not None:
            self.close(pair_id, now)

    def recheck(self, pair_id, seconds):
        """
        Price a pair again in seconds, when a desk waits that long for a
        book to catch up, see Executor.confirm_wait(). A pair keeps one timer,
        the soonest asked for. Outside a running loop, and without books,
        the next change or tick prices it instead.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.books is None:
            return
        due = loop.time() + seconds + RECHECK_MARGIN
        timer = self.rechecks.get(pair_id)
        if timer is not None:
            if timer.when() <= due:
                return
            timer.cancel()
        self.rechecks[pair_id] = loop.call_at(due, self.reprice, pair_id)

    def reprice(self, pair_id):
        """
        A recheck come due: price the pair again if its episode is still open.
        """
        self.rechecks.pop(pair_id, None)
        if pair_id in self.episodes:
            self.update(pair_id, self.books(), now_iso())

    def on_book(self, venue, contract_id, books, now):
        """
        Price every pair this contract belongs to.
        """
        for pair_id in self.by_contract.get((venue, contract_id), ()):
            self.update(pair_id, books, now)

    def tick(self, books, now):
        """
        Price every open episode again, so ones whose books went stale or unseen end.
        """
        for pair_id in list(self.episodes):
            self.update(pair_id, books, now)

    def summary(self):
        """
        One line per kind for the episodes ended since the last summary, then forget them.
        """
        by_kind = defaultdict(list)
        for kind, o in self.finished:
            by_kind[kind].append(o)
        parts = []
        for kind, os in sorted(by_kind.items()):
            best = max(os, key=lambda o: o.peak_profit)
            beat = sum(1 for o in os if o.annual_pct is not None and o.annual_pct >= config.MIN_ANNUAL_PCT)
            parts.append(f"{kind} {len(os)} episodes, {beat} beat target, best {best.peak_profit:.2f}$ for {best.seconds:.0f}s")
        self.finished = []
        return f"scanner: {'; '.join(parts) if parts else 'no episodes'}; {len(self.episodes)} open"
