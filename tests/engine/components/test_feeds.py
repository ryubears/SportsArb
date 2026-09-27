"""
Tests for a venue's feed, run here or in a process of its own.
"""

import asyncio
import time
import scripted_feed
from db import database
from engine.components import feeds, record, streams


class Pipe:
    """
    Stands in for the child's end of the pipe, keeping what was sent.
    """

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def test_the_outbox_keeps_the_newest_book_of_each_contract_and_counts_the_ones_behind_it():
    outbox = feeds.Outbox(Pipe())
    outbox.book("a", [[0.50, 1]], [[0.60, 1]], "t1")
    outbox.book("a", [[0.51, 1]], [[0.60, 1]], "t2")
    outbox.book("b", [[0.40, 1]], [[0.70, 1]], "t3")
    assert outbox.take() == [("books", [("a", [[0.51, 1]], [[0.60, 1]], "t2", 2), ("b", [[0.40, 1]], [[0.70, 1]], "t3", 1)])]
    assert outbox.take() == []


def test_a_gap_goes_out_before_later_books_and_drops_the_unsent_books_of_its_contracts():
    outbox = feeds.Outbox(Pipe())
    outbox.book("a", [[0.5, 1]], [], "t1")         # From the connection that dropped, never sent.
    outbox.book("c", [[0.5, 1]], [], "t1")         # From another connection, which carries on.
    outbox.gap("t0", "t2", ["a", "b"])
    outbox.book("b", [[0.4, 1]], [], "t3")         # From the new connection.
    assert outbox.take() == [("gap", "t0", "t2", ["a", "b"]), ("books", [("c", [[0.5, 1]], [], "t1", 1), ("b", [[0.4, 1]], [], "t3", 1)])]


def test_the_outbox_thread_sends_everything_before_it_closes():
    pipe = Pipe()
    outbox = feeds.Outbox(pipe)
    outbox.start()
    for i in range(1000):
        outbox.book(f"c{i % 10}", [[0.5, i]], [], f"t{i}")
    outbox.close()
    newest = {entry[0]: entry for _, entries in pipe.sent for entry in entries}
    assert {c: entry[1] for c, entry in newest.items()} == {f"c{j}": [[0.5, 990 + j]] for j in range(10)}
    assert sum(entry[4] for _, entries in pipe.sent for entry in entries) == 1000       # Each book counted once, sent or replaced.


async def until(condition, seconds=30):
    """
    Wait for condition() to hold, failing after seconds.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


def test_a_feed_in_its_own_process_follows_the_catalog_and_comes_back_with_a_gap_when_it_dies(tmp_path, monkeypatch):
    monkeypatch.setattr(feeds, "RESTART_SECONDS", (0,))
    conn = database.connect(tmp_path / "t.sqlite")
    r = record.Recorder(conn)
    logs = []
    monkeypatch.setattr(streams, "log", logs.append)

    def held():
        return {contract_id for venue, contract_id in r.books if venue == "polymarket_us"}

    async def scenario():
        s = streams.Streams(r, {"polymarket_us": scripted_feed.ScriptedStream, "kalshi": scripted_feed.ScriptedStream}, processes=True)
        s.start("polymarket_us", ["a", "b", "c"])
        await until(lambda: held() == {"a", "b", "c"})
        assert s.connections("polymarket_us") == 2
        assert s.update({"polymarket_us": ["b", "c", "d"]}) == "polymarket_us +1 -1"
        await until(lambda: held() == {"b", "c", "d"})
        feed = s.feeds["polymarket_us"]
        first = feed.process
        first.kill()
        # Each of the new child's two connections ends a gap from when the first was found dead, stored as one.
        await until(lambda: feed.process is not first and r.gaps["polymarket_us"] == 2 and held() == {"b", "c", "d"})
        await s.stop_all()
        first.join(5)
        return feed, first

    feed, first = asyncio.run(scenario())
    assert feed.process.exitcode == 0 and first.exitcode != 0                   # Stopped when told, the first one killed.
    (gap,) = database.load_gaps(conn, "polymarket_us")
    assert gap.start_ts <= gap.end_ts
    assert r.updates["polymarket_us"] >= 7                                      # a, b, c, then d, then b, c, d again.
    assert [line.split(" process ")[0] for line in logs] == ["polymarket_us feed runs in", "polymarket_us feed", "polymarket_us feed runs in"]
