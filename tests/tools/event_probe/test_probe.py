"""
Tests for the event probe's following of games: the events reads bring, and watching and letting go of a game, with the
leagues' feeds and the venues' connections stood in for.
"""

import asyncio
import sqlite3
import time
from tools.event_probe import leagues, markets, probe
from tools.event_probe.leagues import Game, Snapshot
from tools.event_probe.store import Store

START = 1791410400.0
GAME = Game("mlb", "849822", "2026-10-07", "LAD", "ATL", START, "live")
HITS = markets.Watched("kalshi", "K-HITS", "player_hits", "freddie freeman", 1.5, "yes")
UNDER = markets.Watched("polymarket_us", "pm-hits", "player_hits", "freddie freeman", 0.5, "no")
TOTAL = markets.Watched("kalshi", "K-TOTAL", "total", None, 4.5, "yes")


def read(hits, state="live", away=0, home=0):
    return Snapshot(state, away, home, {"freddie freeman": {"hits": hits}}, play=f"{hits} hits")


def rows(store, table):
    store.flush()
    return store.conn.execute(f"SELECT * FROM {table}").fetchall()


def test_reads_after_the_first_bring_the_settling_the_undoing_and_the_end_of_each_bet(tmp_path):
    store = Store(tmp_path / "probe.sqlite")
    watch = probe.GameWatch(GAME, [HITS, UNDER, TOTAL], store)
    # Already past the under's line when watching began: settled before, so no event.
    assert watch.update(read(1), START + 10, 0.1, 3.0) == []
    events = watch.update(read(2), START + 20, 0.1, None)
    assert [(e[4], e[5], e[6], e[7], e[8]) for e in events] == [("K-HITS", "crossed", "yes", 2, START + 20)]
    events = watch.update(read(1), START + 30, 0.1, None)           # The hit was ruled an error.
    assert [(e[4], e[5], e[6]) for e in events] == [("K-HITS", "reversed", None)]
    events = watch.update(read(1, "final", 3, 1), START + 40, 0.1, None)
    assert [(e[4], e[5], e[6]) for e in events] == [("K-HITS", "final", "no"), ("K-TOTAL", "final", "no")]
    assert len(rows(store, "events")) == 4
    assert [r[7] for r in rows(store, "reads")] == [1, 1, 1, 1]     # Each read's content changed.
    assert not watch.done(START + 40 + probe.AFTER_SECONDS - 1) and watch.done(START + 40 + probe.AFTER_SECONDS)


def test_a_game_that_never_begins_is_let_go_long_after_its_start(tmp_path):
    watch = probe.GameWatch(GAME, [], Store(tmp_path / "probe.sqlite"))
    watch.update(Snapshot("pre", 0, 0), START, 0.1, None)
    assert not watch.done(START + probe.MAX_PRE_SECONDS - 1) and watch.done(START + probe.MAX_PRE_SECONDS)


class Feed:
    """
    Stands in for a venue's connections, keeping the contracts added and removed.
    """

    def __init__(self):
        self.added, self.removed = [], []

    def add(self, contract_ids):
        self.added += contract_ids

    def remove(self, contract_ids):
        self.removed += contract_ids


def test_a_game_near_its_start_is_watched_until_its_feed_is_done_and_its_books_are_kept(tmp_path, monkeypatch):
    catalog = sqlite3.connect(":memory:")
    near = Game("mlb", "1", "2026-10-07", "LAD", "ATL", START, "live")
    later = Game("mlb", "2", "2026-10-07", "TB", "NYY", START + 3 * 3600, "pre")
    over = Game("mlb", "3", "2026-10-07", "MIL", "SD", START - 4 * 3600, "final")
    monkeypatch.setattr(leagues, "games", lambda reader, sport, day: [near, later, over])
    monkeypatch.setattr(markets, "load_watched", lambda conn, game: [HITS, UNDER])
    reads = iter([read(0), read(2), read(2, "final", 3, 1)])
    monkeypatch.setattr(leagues, "read_game", lambda reader, game: (next(reads), time.time(), 0.1, None))
    monkeypatch.setattr(probe, "AFTER_SECONDS", 0)
    store = Store(tmp_path / "probe.sqlite")
    p = probe.Probe(store, catalog, ["mlb"], poll_seconds=0, log=lambda line: None)
    p.feeds = {"kalshi": Feed(), "polymarket_us": Feed()}

    async def run():
        await p.schedule(START)
        assert list(p.watches) == [("mlb", "1")]
        await asyncio.wait_for(p.tasks[("mlb", "1")], 5)
    asyncio.run(run())
    assert p.watches == {}
    assert (p.feeds["kalshi"].added, p.feeds["kalshi"].removed) == (["K-HITS"], ["K-HITS"])
    assert (p.feeds["polymarket_us"].added, p.feeds["polymarket_us"].removed) == (["pm-hits"], ["pm-hits"])
    assert [(e[4], e[5]) for e in rows(store, "events")] == [("K-HITS", "crossed"), ("pm-hits", "crossed")]
    assert rows(store, "watched")[0][:4] == ("kalshi", "K-HITS", "mlb", "1")
    # What the venues send of the watched contracts is kept as it comes.
    p.on_book("kalshi")("K-HITS", [[0.4, 10.0]], [[0.45, 5.0]], "2026-10-07T22:00:00+00:00", sent=START)
    p.on_state("kalshi")("K-HITS", "paused")
    p.on_gap("polymarket_us")("2026-10-07T22:00:00+00:00", "2026-10-07T22:00:01+00:00", ["pm-hits"])
    book = rows(store, "books")[0]
    assert (book[0], book[1], book[3], book[4], book[5]) == ("kalshi", "K-HITS", START, "[[0.4, 10.0]]", "[[0.45, 5.0]]")
    assert rows(store, "states")[0][3] == "paused" and rows(store, "gaps")[0][3] == 1
