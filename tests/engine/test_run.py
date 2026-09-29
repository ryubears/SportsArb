"""
Tests for the live process's refresh loop.
"""

import asyncio
import multiprocessing
import os
import pytest
import scripted_refresh
from db import database
from engine import run
from engine.components.market import streams
from engine.components.money import live as money_live
from engine.components.trading import brakes, notify
from engine.helper import config


def test_run_survives_a_failing_refresh(tmp_path, monkeypatch, capsys, fake_stream):
    def broken_refresh(sport, log=print, db_path=None):
        raise RuntimeError("kalshi is down")
    monkeypatch.setattr(run.pipeline, "refresh", broken_refresh)
    # In this process, where the broken refresh is patched in. A child would import the real one.
    monkeypatch.setattr(run, "refresh_in_child", lambda sports: asyncio.to_thread(run.refresh_catalog, sports, run.log))
    for venue in streams.STREAMS:
        monkeypatch.setitem(streams.STREAMS, venue, fake_stream)
    conn = database.connect(tmp_path / "test.sqlite")
    asyncio.run(run.run(conn, run.RunOptions(sports=("nfl",), seconds=3, catalog_seconds=1)))
    out = capsys.readouterr().out
    assert "starting nfl, code " in out.splitlines()[0]
    failed = "nfl catalog refresh failed (RuntimeError('kalshi is down')), keeping its stored catalog"
    assert out.count(failed) >= 2                                                   # Before starting, and at the next refresh.
    assert "    RuntimeError: kalshi is down" in out                                  # With the traceback.
    assert "catalog refreshed, nfl: failed" in out


def test_a_sport_whose_refresh_fails_leaves_the_others_to_refresh(monkeypatch):
    def refresh(sport, log=print, db_path=None):
        if sport == "ncaaf":
            raise RuntimeError("cfb tag gone")
        return "1 pairs"
    monkeypatch.setattr(run.pipeline, "refresh", refresh)
    logs = []
    assert run.refresh_catalog(("nfl", "ncaaf"), logs.append) == "nfl: 1 pairs; ncaaf: failed"
    assert logs[0].startswith("ncaaf catalog refresh failed (RuntimeError('cfb tag gone')), keeping its stored catalog")


def test_the_refresh_runs_in_a_child_process_that_is_gone_once_it_answers():
    line = asyncio.run(run.refresh_in_child(("nfl", "ncaaf"), scripted_refresh.refresh))
    assert line.startswith("refreshed nfl, ncaaf in process ")
    assert int(line.split()[-1]) != os.getpid()
    assert multiprocessing.active_children() == []


def test_a_refresh_cancelled_as_the_run_stops_takes_its_child_down():
    async def stop_midway():
        refresh = asyncio.create_task(run.refresh_in_child(("nfl",), scripted_refresh.slow))
        await asyncio.sleep(0.5)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
    asyncio.run(stop_midway())
    assert multiprocessing.active_children() == []


def test_a_refresh_process_that_dies_without_answering_fails_the_refresh():
    with pytest.raises(RuntimeError, match="the refresh process ended without an answer, exit code 3"):
        asyncio.run(run.refresh_in_child(("nfl",), scripted_refresh.crash))
    assert multiprocessing.active_children() == []


def session(tmp_path, monkeypatch, fake_stream, **kwargs):
    for venue in streams.STREAMS:
        monkeypatch.setitem(streams.STREAMS, venue, fake_stream)
    return run.Session(database.connect(tmp_path / "test.sqlite"), ("nfl",), **kwargs)


def test_session_without_scanning_or_trading_only_records(tmp_path, monkeypatch, capsys, fake_stream):
    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream, with_scanner=False)
        s.start()
        s.tick()
        await s.close()
        return s
    s = asyncio.run(scenario())
    assert (s.scanner, s.desks) == (None, [])
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
    assert heads(on_tick) == heads(on_close) == ["scanner", "paper", "paper settled", "paper capital", "tracking 0 books"]


def test_a_trading_session_logs_its_settings_when_it_starts(tmp_path, monkeypatch, capsys, fake_stream):
    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream)
        s.start()
        await s.close()
    asyncio.run(scenario())
    first = capsys.readouterr().out.splitlines()[0]
    assert first[9:].startswith("settings: min edge 0.05$, fill share 0.5, rejects 3%")


def test_a_session_trading_both_modes_keeps_a_desk_for_each_and_offers_live_the_signal_first(tmp_path, monkeypatch, capsys, fake_stream):
    monkeypatch.setattr(money_live, "READERS", {"kalshi": lambda: 800.0, "polymarket_us": lambda: 600.0})
    monkeypatch.setattr(brakes, "HALT_FILE", tmp_path / "live_halt.txt")
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "email.json")

    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream, executors=run.EXECUTE["both"])
        s.start()
        s.last_summary = 0
        s.tick()                                    # The first tick starts reading the live balances.
        await s.desks[0].cash.readings.running
        s.tick()
        await s.close()
        return s
    s = asyncio.run(scenario())
    assert [d.mode for d in s.desks] == ["live", "paper"]
    assert s.scanner.on_signals == [s.desks[0].executor.signal, s.desks[1].executor.signal]
    assert s.desks[0].cash.amounts == {"kalshi": 800.0, "polymarket_us": 600.0}
    assert s.desks[1].cash.amounts == {"kalshi": 10000.0, "polymarket_us": 10000.0}         # Paper money is its own.
    out = [line[9:] for line in capsys.readouterr().out.splitlines()]                       # Past the timestamp.
    live_caps = f"cap {config.LIVE_MIN_CAP} to {config.LIVE_MAX_CAP} contracts"
    assert out[0].startswith("settings: min edge") and out[1].startswith(f"LIVE TRADING with real money: {live_caps}")
    assert out[2] == f"no email settings in {tmp_path / 'email.json'}, alerts are only logged and stored"
    assert "live balances read: kalshi 800$, polymarket_us 600$" in out
    heads = [line.split(":")[0] for line in out]
    assert {"live", "live settled", "live capital", "paper", "paper settled", "paper capital"} <= set(heads)
