"""
Tests for the recorder's write rules, its stream management, and its refresh loop.
"""

import asyncio
import record
from db import database


class FakeStream:
    """
    A stand in for a venue BookStream that records what it was asked to do and never connects.
    """
    instances = []

    def __init__(self, contract_ids, on_book, log):
        self.wanted = set(contract_ids)
        self.on_book = on_book
        self.added, self.removed = [], []
        FakeStream.instances.append(self)

    def add(self, contract_ids):
        self.wanted |= set(contract_ids)
        self.added.append(sorted(contract_ids))

    def remove(self, contract_ids):
        self.wanted -= set(contract_ids)
        self.removed.append(sorted(contract_ids))

    async def run(self):
        await asyncio.sleep(3600)


# WRITE RULES

def test_flush_writes_only_when_the_best_level_changes(tmp_path):
    conn = database.connect(tmp_path / "test.sqlite")
    r = record.Recorder(conn)
    r.on_book("kalshi", "T1", [[0.50, 10], [0.49, 5]], [[0.52, 7]])
    r.flush()
    r.on_book("kalshi", "T1", [[0.50, 10], [0.48, 5]], [[0.52, 7]])   # Only a deeper level moved.
    r.flush()
    r.on_book("kalshi", "T1", [[0.50, 11]], [[0.52, 7]])              # Size at the best level moved.
    r.flush()
    assert r.rows_written == 2
    rows = database.load_quotes(conn, "kalshi", ["T1"])["T1"]
    assert [q.bids for q in rows] == [[[0.50, 10], [0.49, 5]], [[0.50, 11]]]


def test_books_are_trimmed_to_the_kept_levels(tmp_path):
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    r.on_book("polymarket_us", "T", [[0.5 - i / 100, 1] for i in range(10)], [[0.51, 1]])
    assert len(r.latest[("polymarket_us", "T")].bids) == record.LEVELS


def test_status_reports_time_since_each_venue_updated(tmp_path):
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    assert "last never" in r.status()
    r.on_book("kalshi", "T", [[0.5, 1]], [[0.6, 1]])
    assert "kalshi 1 (last 0s ago)" in r.status()


# STREAMS

def test_streams_change_subscriptions_in_place(tmp_path):
    async def scenario():
        FakeStream.instances.clear()
        r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
        streams = record.Streams(r, {"polymarket_us": FakeStream, "kalshi": FakeStream})
        streams.start("polymarket_us", ["a", "b"])
        streams.start("kalshi", ["k1"])
        await asyncio.sleep(0)
        summary = streams.update({"polymarket_us": ["a", "c"], "kalshi": ["k1"]})
        pm = streams.streams["polymarket_us"]
        pm.on_book("b", [[0.5, 1]], [[0.6, 1]])    # A late update for the removed contract.
        pm.on_book("a", [[0.5, 1]], [[0.6, 1]])
        await streams.stop_all()
        return summary, pm, r.latest
    summary, pm, latest = asyncio.run(scenario())
    assert summary == "polymarket_us +1 -1"
    assert len(FakeStream.instances) == 2                 # No connection was replaced.
    assert (pm.added, pm.removed, pm.wanted) == ([["c"]], [["b"]], {"a", "c"})
    assert ("polymarket_us", "b") not in latest
    assert ("polymarket_us", "a") in latest


# REFRESH LOOP

def test_run_survives_a_failing_refresh(tmp_path, monkeypatch, capsys):
    def broken_refresh(sport, log=print, db_path=None):
        raise RuntimeError("kalshi is down")
    monkeypatch.setattr(record.pipeline, "refresh", broken_refresh)
    for venue in record.STREAMS:
        monkeypatch.setitem(record.STREAMS, venue, FakeStream)
    conn = database.connect(tmp_path / "test.sqlite")
    asyncio.run(record.run(conn, "nfl", seconds=3, catalog_seconds=1))
    out = capsys.readouterr().out
    assert "catalog refresh failed (RuntimeError('kalshi is down')), starting with the stored catalog" in out
    assert "catalog refresh failed (RuntimeError('kalshi is down')), keeping current subscriptions" in out
