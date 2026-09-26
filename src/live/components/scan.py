"""
Find arbitrage episodes in pairs as the recorder's books change.

Whenever a member's book changes, the scanner finds the cheapest way to
hold yes and the cheapest way to hold no across the pair's members, on
any venues, and prices buying both. An episode is a stretch where that
net edge stays above zero after fees. Each episode becomes an Opportunity
with its two legs, its duration, its peak edge, how many contracts could
have been filled at the peak by walking the recorded depth, and the return
on the capital tied up, annualized as if held until the bet pays out.

The recorder drives the Scanner with the books it holds in memory and
the Scanner stores every episode as it ends, so the opportunities table
is the log of everything it saw. The summary script reads it. The
pricing itself lives in pricing.py.
"""

from collections import defaultdict
from common import config
from common.timeutil import now_iso, seconds_between
from db import database
from db.models import Opportunity
from common.gametime import pays_at as payout_time
from live.price.pricing import best_trade, trade_words


# EPISODES

def finish(pair, peak, start_ts, end_ts):
    """
    Turn an in progress episode into an Opportunity. peak holds the best moment seen so far.
    """
    start_time = next((m["start_time"] for m in pair["members"] if m["start_time"]), None)
    live = 1 if start_time and peak["ts"] >= start_time else 0
    # Capital is locked until the slower of the two legs pays, so the later resolution counts.
    pays_at = payout_time((peak["yes"], peak["no"]))
    days_held = max(seconds_between(peak["ts"], pays_at) / 86400, 1 / 24) if pays_at else None
    return_pct = 100 * peak["edge"] / (1 - peak["edge"])
    return Opportunity(
        pair_id=pair["id"],
        trade=trade_words(peak["yes"], peak["no"]),
        yes_venue=peak["yes"]["venue"],
        yes_contract=peak["yes"]["contract_id"],
        no_venue=peak["no"]["venue"],
        no_contract=peak["no"]["contract_id"],
        start_ts=start_ts,
        end_ts=end_ts,
        seconds=seconds_between(start_ts, end_ts),
        peak_ts=peak["ts"],
        peak_edge=peak["edge"],
        peak_size=peak["size"],
        peak_profit=peak["profit"],
        live=live,
        days_held=days_held,
        return_pct=return_pct,
        annual_pct=return_pct * 365 / days_held if days_held else None,
    )


class Scanner:
    """
    Tracks episodes for the pairs of a sport from a map of the newest
    books, keyed by (venue, contract_id). Only the pairs a contract belongs
    to are priced when its book changes. A member is left out while its book
    is older than config.MAX_QUOTE_AGE or missing from the map, which is how the
    recorder says a venue's books went unseen. Groups and fee schedules come
    from the database and are reloaded after each catalog refresh. Every
    finished episode is stored at once, and the big ones are logged. With
    on_signal, the first moment of each episode that the callback accepts
    becomes a trade, see execute.py.
    """

    def __init__(self, conn, sport, log=print, on_signal=None):
        self.conn = conn
        self.sport = sport
        self.log = log
        self.on_signal = on_signal          # Called once per episode with the trade to make, if it wants it.
        self.episodes = {}                  # pair id maps to {"start_ts", "peak", "pair"} while an edge is open.
        self.finished = []                  # (kind, Opportunity) for episodes ended since the last summary.
        self.reload()

    def reload(self):
        """
        Load the pairs and fee schedules again, ending open episodes of pairs that are gone.
        """
        self.fee_infos = defaultdict(dict, database.load_fee_infos(self.conn, self.sport))
        self.pairs = database.load_pairs(self.conn, self.sport)
        by_contract = defaultdict(list)
        for pair_id, pair in self.pairs.items():
            for m in pair["members"]:
                by_contract[(m["venue"], m["contract_id"])].append(pair_id)
        self.by_contract = dict(by_contract)
        for pair_id in [pair_id for pair_id in self.episodes if pair_id not in self.pairs]:
            self.close(pair_id, now_iso())

    def price(self, pair, latest, now):
        """
        The best trade across the pair's members whose books are fresh, as a Priced, or None.
        """
        members = [m for m in pair["members"]
                   if (m["venue"], m["contract_id"]) in latest
                   and seconds_between(latest[(m["venue"], m["contract_id"])].ts, now) <= config.MAX_QUOTE_AGE]
        if len(members) < 2:
            return None
        return best_trade(members, latest, self.fee_infos)

    def on_book(self, venue, contract_id, latest, now):
        """
        Price every pair this contract belongs to.
        """
        for pair_id in self.by_contract.get((venue, contract_id), ()):
            self.update(pair_id, latest, now)

    def tick(self, latest, now):
        """
        Price every open episode again, so ones whose books went stale or unseen end.
        """
        for pair_id in list(self.episodes):
            self.update(pair_id, latest, now)

    def update(self, pair_id, latest, now):
        """
        Open, extend, or end the episode for one pair from the current books.
        """
        pair = self.pairs[pair_id]
        priced = self.price(pair, latest, now)
        episode = self.episodes.get(pair_id)
        if priced is not None and priced.edge > 0:
            if episode is None:
                episode = self.episodes[pair_id] = {"start_ts": now, "peak": None, "pair": pair}
            if episode["peak"] is None or priced.edge > episode["peak"]["edge"]:
                episode["peak"] = dict(priced._asdict(), ts=now)
            if (self.on_signal and not episode.get("traded")
                    and self.on_signal(pair, priced.yes, priced.no, priced.edge, priced.size, self.fee_infos, now)):
                episode["traded"] = True
        elif episode is not None:
            self.close(pair_id, now)

    def close(self, pair_id, now):
        """
        End an episode, store it, and log it when it was worth something.
        """
        episode = self.episodes.pop(pair_id)
        o = finish(episode["pair"], episode["peak"], episode["start_ts"], now)
        database.insert_opportunities(self.conn, [o])
        self.finished.append((episode["pair"]["kind"], o))
        if o.peak_profit >= config.LOG_PROFIT_DOLLARS:
            self.log(f"episode {episode['pair']['label']}: {o.trade}, {100 * o.peak_edge:.1f}c x {o.peak_size:.0f} = {o.peak_profit:.2f}$, lasted {o.seconds:.1f}s")

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
            beat = sum(1 for o in os if o.annual_pct is not None and o.annual_pct >= config.TARGET_ANNUAL_PCT)
            parts.append(f"{kind} {len(os)} episodes, {beat} beat target, best {best.peak_profit:.2f}$ for {best.seconds:.0f}s")
        self.finished = []
        return f"scanner: {'; '.join(parts) if parts else 'no episodes'}; {len(self.episodes)} open"
