"""
Follow MLB and NHL games through the leagues' free feeds and, at the same
time, the venues' books on every cataloged contract of those games, and
keep both, to measure whether the feeds see a play before the venues'
prices move. It trades nothing, and reads the bot's database only for
the catalog. See the package's docstring for the idea, and report.py for
what the records show.

A game is watched from LEAD_SECONDS before its scheduled start until
AFTER_SECONDS after its feed says it is final. Its feed is read every
POLL_SECONDS, and every change of a watched book is kept, DEPTH levels a
side. A read that settles a contract's bet, or undoes that, is an event,
see markets.statement(). The first read of a game is the baseline, since
what was settled before then was settled before the probe watched.

Run it on the instance beside the bot, at a lower priority so it never
holds up the bot's feeds, from src/, with -u so each log line reaches the
file at once rather than when Python's buffer fills:
    nohup nice -n 10 python3 -u -m tools.event_probe.probe >> ../data/event_probe.log 2>&1 &
Stop it with pkill -f tools.event_probe.probe. Rows are written each
second, so at most the last second is lost.
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from common.log import log, on_failure
from common.timeutil import at_seconds, eastern_date
from common.venues import VENUES
from db import database
from engine.components.market.feeds import VenueFeed
from engine.components.market.streams import STREAMS
from tools.event_probe import leagues, markets
from tools.event_probe.store import PATH, Store

SPORTS = ("mlb", "nhl")
POLL_SECONDS = 1.0          # Between reads of a game's feed, well under either cache's max-age, so a change is seen within a second of the cache's.
SCHEDULE_SECONDS = 300      # Between reads of the schedules.
LEAD_SECONDS = 600          # A game is watched from this long before its scheduled start,
AFTER_SECONDS = 600         # until this long after its feed says it is final, to keep the venues' last moves,
MAX_PRE_SECONDS = 3 * 3600  # or this long after its scheduled start while it has not begun, as for a game put off.
DEPTH = 10                  # Levels a side kept of each book. An order left at an old price may sit below the best few.
STATUS_SECONDS = 60         # Between status lines.


def days(now):
    """
    Yesterday and today in US Eastern time, as the leagues date their games, so a game running past midnight is still found.
    """
    return [eastern_date(at_seconds(now - 86400)), eastern_date(at_seconds(now))]


class GameWatch:
    """
    One game: its contracts, what the last read said of each one's bet, and the events reads bring.
    """

    def __init__(self, game, watched, store):
        self.game = game
        self.watched = watched
        self.store = store
        self.truths = {}            # Each contract's key maps to its bet's truth at the last read, see markets.statement().
        self.baseline = None        # The first read's arrival, None before it.
        self.last = None            # The last read's content, to mark reads that changed it.
        self.final_since = None     # When a read first said the game is final.
        self.pre = True             # Whether the last read said the game has not begun.

    def update(self, snap, arrived, seconds, age):
        """
        Keep a read and the events it brings, and return those as rows of the events table.
        """
        content = (snap.state, snap.away, snap.home, snap.players, snap.pulled, snap.made, snap.play)
        g = self.game
        self.store.add("reads", (g.sport, g.game_id, arrived, seconds, age, snap.made, snap.play_ended, int(content != self.last),
                                 snap.state, snap.away, snap.home, snap.play))
        self.last = content
        events = []
        for bet in self.watched:
            truth = markets.statement(bet, snap, g)
            before = self.truths.get((bet.venue, bet.contract_id))
            self.truths[(bet.venue, bet.contract_id)] = truth
            if self.baseline is None or truth == before:
                continue
            why = "reversed" if before is not None else "final" if snap.state == "final" else "crossed" if truth else "pulled"
            events.append((None, g.sport, g.game_id, bet.venue, bet.contract_id, why, markets.winner(bet, truth),
                           markets.count(bet, snap, g), arrived, snap.play_ended, snap.made, snap.play))
        for row in events:
            self.store.add("events", row)
        if self.baseline is None:
            self.baseline = arrived
        if snap.state == "final" and self.final_since is None:
            self.final_since = arrived
        self.pre = snap.state == "pre"
        return events

    def done(self, now):
        """
        Whether the game needs no more reads: final long enough, or not begun long after its start.
        """
        if self.final_since is not None:
            return now - self.final_since >= AFTER_SECONDS
        return self.pre and now - self.game.start >= MAX_PRE_SECONDS


class Probe:
    """
    The games watched, a venue feed each, and the store their records go to.
    """

    def __init__(self, store, catalog, sports=SPORTS, poll_seconds=POLL_SECONDS, log=log):
        self.store = store
        self.catalog = catalog
        self.sports = sports
        self.poll_seconds = poll_seconds
        self.log = log
        self.feeds = {venue: VenueFeed(STREAMS[venue], self.on_book(venue), self.on_gap(venue), log, depth=DEPTH,
                                       on_state=self.on_state(venue)) for venue in VENUES}
        self.watches = {}           # (sport, game id) maps to its GameWatch.
        self.tasks = {}             # The same keys map to the task reading the game's feed.
        self.counts = Counter()     # Reads, books, and events since the last status line.

    def on_book(self, venue):
        def handle(contract_id, bids, asks, ts, sent=None):
            self.store.add("books", (venue, contract_id, time.time(), sent, json.dumps(bids), json.dumps(asks)))
            self.counts["books"] += 1
        return handle

    def on_gap(self, venue):
        return lambda start_ts, end_ts, contract_ids: self.store.add("gaps", (venue, start_ts, end_ts, len(contract_ids)))

    def on_state(self, venue):
        return lambda contract_id, why: self.store.add("states", (venue, contract_id, time.time(), why))

    def watch(self, game, now):
        """
        Start following a game: its contracts' books and its feed.
        """
        watched = markets.load_watched(self.catalog, game)
        self.store.add("games", (game.sport, game.game_id, game.game_date, game.away, game.home, game.start, now))
        for w in watched:
            self.store.add("watched", (w.venue, w.contract_id, game.sport, game.game_id, w.kind, w.subject, w.line, w.polarity,
                                       json.dumps(w.fee_info)))
        key = (game.sport, game.game_id)
        self.watches[key] = GameWatch(game, watched, self.store)
        by_venue = {venue: [w.contract_id for w in watched if w.venue == venue] for venue in VENUES}
        for venue, contract_ids in by_venue.items():
            if contract_ids:
                self.feeds[venue].add(contract_ids)
        self.tasks[key] = asyncio.create_task(self.follow(key))
        self.tasks[key].add_done_callback(on_failure(self.log, f"following {game.sport} game {game.game_id}"))
        self.log(f"watching {game.sport} {game.away}@{game.home} ({game.game_id}), "
                 + ", ".join(f"{len(ids)} {venue} contracts" for venue, ids in by_venue.items()))

    def unwatch(self, key):
        """
        Stop following a game, once its feed needs no more reads.
        """
        watch = self.watches.pop(key)
        self.tasks.pop(key, None)
        for venue in VENUES:
            self.feeds[venue].remove([w.contract_id for w in watch.watched if w.venue == venue])
        self.log(f"done with {watch.game.sport} {watch.game.away}@{watch.game.home} ({watch.game.game_id})")

    async def follow(self, key):
        """
        Read a game's feed every poll_seconds until it is done. A failed read
        is logged once until reads work again, and the next one comes on time.
        """
        watch, reader, failing = self.watches[key], leagues.Reader(), False
        while not watch.done(time.time()):
            started = time.time()
            try:
                snap, arrived, seconds, age = await asyncio.to_thread(leagues.read_game, reader, watch.game)
                events = watch.update(snap, arrived, seconds, age)
                self.counts["reads"] += 1
                self.counts["events"] += len(events)
                self.log_events(watch.game, snap, events)
                if failing:
                    self.log(f"reads of {watch.game.sport} game {watch.game.game_id} work again")
                failing = False
            except Exception as e:
                if not failing:
                    self.log(f"read of {watch.game.sport} game {watch.game.game_id} failed ({type(e).__name__}: {str(e)[:120]})")
                failing = True
            await asyncio.sleep(max(0.0, self.poll_seconds - (time.time() - started)))
        self.unwatch(key)

    def log_events(self, game, snap, events):
        """
        Log a read's events: each one a play settled or undid, and the game's end in one line, since it settles every contract.
        """
        finals = 0
        for _, _, _, venue, contract_id, why, paid, value, _, _, _, play in events:
            if why == "final":
                finals += 1
            else:
                self.log(f"{game.sport} {game.away}@{game.home} {why}: {venue} {contract_id} pays {paid} at {value}, after {play}")
        if finals:
            self.log(f"{game.sport} {game.away}@{game.home} final, {snap.away}-{snap.home}: {finals} contracts settled")

    async def schedule(self, now):
        """
        Read the schedules and start watching every game near its start and not yet final.
        """
        for sport in self.sports:
            for day in days(now):
                try:
                    games = await asyncio.to_thread(leagues.games, leagues.Reader(), sport, day)
                except Exception as e:
                    self.log(f"{sport} schedule for {day} failed ({type(e).__name__}: {str(e)[:120]})")
                    continue
                for game in games:
                    if (sport, game.game_id) not in self.watches and game.state != "final" and game.start - LEAD_SECONDS <= now:
                        self.watch(game, now)

    def status(self):
        contracts = Counter(w.venue for watch in self.watches.values() for w in watch.watched)
        games = ", ".join(f"{w.game.away}@{w.game.home}" for w in self.watches.values()) or "none"
        self.log(f"watching {len(self.watches)} games ({games}), " + ", ".join(f"{contracts[v]} {v}" for v in VENUES)
                 + f" contracts; last {STATUS_SECONDS}s: {self.counts['reads']} reads, {self.counts['books']} books, "
                 + f"{self.counts['events']} events")
        self.counts = Counter()

    async def run(self, hours=None):
        for feed in self.feeds.values():
            feed.start([])
        stop = time.time() + hours * 3600 if hours else None
        last_schedule = last_status = 0
        try:
            while stop is None or time.time() < stop:
                now = time.time()
                if now - last_schedule >= SCHEDULE_SECONDS:
                    last_schedule = now
                    await self.schedule(now)
                if now - last_status >= STATUS_SECONDS:
                    last_status = now
                    self.status()
                self.store.flush()
                await asyncio.sleep(1)
        finally:
            for task in self.tasks.values():
                task.cancel()
            for feed in self.feeds.values():
                await feed.stop()
            self.store.flush()


def main():
    parser = argparse.ArgumentParser(description="Follow games' feeds and their contracts' books, trading nothing.")
    parser.add_argument("--sports", default=",".join(SPORTS), help="Leagues to follow, of " + ", ".join(SPORTS))
    parser.add_argument("--poll", type=float, default=POLL_SECONDS, help="Seconds between reads of a game's feed")
    parser.add_argument("--hours", type=float, help="Stop after this long; runs until killed without it")
    parser.add_argument("--db", help=f"Where to keep the records, {PATH} without it")
    args = parser.parse_args()
    store = Store(Path(args.db) if args.db else PATH)
    log(f"event probe starting: {args.sports}, a read every {args.poll}s, records in {args.db or PATH}")
    asyncio.run(Probe(store, database.read_only(), args.sports.split(","), args.poll).run(args.hours))


if __name__ == "__main__":
    main()
