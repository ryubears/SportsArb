"""
Tests for overriding settings for one run.
"""

import pytest
from engine import run
from engine.components.money.paper import PaperBalances
from engine.helper import config, game


@pytest.fixture
def restore(monkeypatch):
    """
    Put every setting back after the test, whatever it overrode.
    """
    for name in [n for n in vars(config) if n.isupper()]:
        monkeypatch.setattr(config, name, getattr(config, name))


def test_override_reads_each_value_as_the_settings_own_type(restore):
    config.override(["min_edge=0.03", "MIN_PAYOUT_HOURS=48", " settle_hours = 1.5 ", "feed_processes=False"])
    assert (config.MIN_EDGE, config.MIN_PAYOUT_HOURS, config.SETTLE_HOURS, config.FEED_PROCESSES) == (0.03, 48, 1.5, False)
    assert isinstance(config.MIN_PAYOUT_HOURS, int)
    assert game.money_back("2026-09-20T20:00:00+00:00") == "2026-09-20T21:30:00+00:00"     # What depends on a setting follows it.


@pytest.mark.parametrize("assignment, message", [
    ("min_edge", "unknown setting"),
    ("no_such_setting=1", "unknown setting"),
    ("override=1", "unknown setting"),
    ("min_payout_hours=1.5", "MIN_PAYOUT_HOURS needs a whole number"),
    ("live_max_cap=5", "unknown setting"),                     # Gone: no trade has a cap.
    ("paper_order_ms=5", "not a single number"),
    ("game_hours=3.5", "not a single number"),                 # One for each sport, so set in config.py.
    ("feed_processes=0", "FEED_PROCESSES needs true or false"),
])
def test_override_refuses_what_it_cannot_set(restore, assignment, message):
    with pytest.raises(ValueError, match=message):
        config.override([assignment])


def test_settings_are_read_when_used_not_when_imported(restore, tmp_path):
    from db import database
    config.override(["paper_start_balance=2500"])
    assert PaperBalances(database.connect(tmp_path / "t.sqlite")).amounts == {"kalshi": 2500.0, "polymarket_us": 2500.0}
    assert "min edge 0.02$" in run.trading_settings() and "start balance 2,500$" in run.paper_settings()


def test_the_command_line_rejects_a_bad_setting():
    import subprocess, sys
    from pathlib import Path
    src = Path(config.__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-m", "engine.run", "--set", "min_edge=cheap"], cwd=src, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "MIN_EDGE needs a number, not 'cheap'" in done.stderr
