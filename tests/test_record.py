"""
Tests for the recorder's write rules.
"""

import asyncio
import record
from db import database


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
    r.on_book("polymarket", "T", [[0.5 - i / 100, 1] for i in range(10)], [[0.51, 1]])
    assert len(r.latest[("polymarket", "T")].bids) == record.LEVELS


def test_status_reports_time_since_each_venue_updated(tmp_path):
    r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
    assert "last never" in r.status()
    r.on_book("kalshi", "T", [[0.5, 1]], [[0.6, 1]])
    assert "kalshi 1 (last 0s ago)" in r.status()


def fake_streamer(started):
    """
    A stand in for a venue stream that records what it was asked to subscribe to and then waits forever.
    """
    async def stream(contract_ids, on_book, log):
        started.append(list(contract_ids))
        await asyncio.sleep(3600)
    return stream


def test_streams_add_new_contracts_without_touching_existing_connections(tmp_path):
    async def scenario():
        r = record.Recorder(database.connect(tmp_path / "test.sqlite"))
        started = {"polymarket": [], "kalshi": []}
        streams = record.Streams(r, {v: fake_streamer(started[v]) for v in started})
        streams.start("polymarket", ["a", "b"])
        streams.start("kalshi", ["k1"])
        await asyncio.sleep(0)
        summary = streams.update({"polymarket": ["a", "c"], "kalshi": ["k1"]})
        await asyncio.sleep(0)
        # The dropped contract is still streamed but no longer kept.
        callback = streams.tasks[0].get_coro().cr_frame.f_locals["on_book"]
        callback("b", [[0.5, 1]], [[0.6, 1]])
        callback("a", [[0.5, 1]], [[0.6, 1]])
        await streams.stop_all()
        return summary, started, r.latest
    summary, started, latest = asyncio.run(scenario())
    assert summary == "polymarket +1 -1"
    assert started["polymarket"] == [["a", "b"], ["c"]]
    assert started["kalshi"] == [["k1"]]
    assert ("polymarket", "b") not in latest
    assert ("polymarket", "a") in latest
