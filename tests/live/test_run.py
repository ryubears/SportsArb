"""
Tests for the live process's refresh loop.
"""

import asyncio
from db import database
from live import run, streams


def test_run_survives_a_failing_refresh(tmp_path, monkeypatch, capsys, fake_stream):
    def broken_refresh(sport, log=print, db_path=None):
        raise RuntimeError("kalshi is down")
    monkeypatch.setattr(run.pipeline, "refresh", broken_refresh)
    for venue in streams.STREAMS:
        monkeypatch.setitem(streams.STREAMS, venue, fake_stream)
    conn = database.connect(tmp_path / "test.sqlite")
    asyncio.run(run.run(conn, "nfl", seconds=3, catalog_seconds=1))
    out = capsys.readouterr().out
    assert "catalog refresh failed (RuntimeError('kalshi is down')), starting with the stored catalog" in out
    assert "catalog refresh failed (RuntimeError('kalshi is down')), keeping current subscriptions" in out


def session(tmp_path, monkeypatch, fake_stream, **kwargs):
    for venue in streams.STREAMS:
        monkeypatch.setitem(streams.STREAMS, venue, fake_stream)
    return run.Session(database.connect(tmp_path / "test.sqlite"), "nfl", **kwargs)


def test_session_without_scanning_or_trading_only_records(tmp_path, monkeypatch, capsys, fake_stream):
    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream, with_scanner=False)
        s.start()
        s.tick()
        await s.close()
        return s
    s = asyncio.run(scenario())
    assert (s.scanner, s.executor, s.settler, s.rebalancer) == (None, None, None, None)
    out = capsys.readouterr().out
    assert "nothing to record, run pipeline.py first" in out
    assert "tracking 0 books" in out and "scanner:" not in out and "paper:" not in out


def test_session_logs_every_components_summary_when_due_and_on_close(tmp_path, monkeypatch, capsys, fake_stream):
    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream)
        s.start()
        capsys.readouterr()
        s.last_summary = s.last_status = 0        # Everything is overdue at the first tick.
        s.tick()
        on_tick = capsys.readouterr().out
        await s.close()
        return on_tick, capsys.readouterr().out

    def heads(out):
        return [line[9:].split(":")[0].split(",")[0] for line in out.splitlines()]      # Past the timestamp.
    on_tick, on_close = asyncio.run(scenario())
    assert heads(on_tick) == heads(on_close) == ["scanner", "paper", "settled", "tracking 0 books"]
