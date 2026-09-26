"""
Tests for overriding settings for one run.
"""

import pytest
from common import config, game
from live import run
from live.components import balances


@pytest.fixture
def restore(monkeypatch):
    """
    Put every setting back after the test, whatever it overrode.
    """
    for name in [n for n in vars(config) if n.isupper()]:
        monkeypatch.setattr(config, name, getattr(config, name))


def test_override_reads_each_value_as_the_settings_own_type(restore):
    config.override(["min_edge=0.03", "MAX_CAP=100", " game_hours = 3.5 "])
    assert (config.MIN_EDGE, config.MAX_CAP, config.GAME_HOURS) == (0.03, 100, 3.5)
    assert isinstance(config.MAX_CAP, int)
    assert game.payout_hours() == 3.5 + config.SETTLE_HOURS          # What depends on a setting follows it.


@pytest.mark.parametrize("assignment, message", [
    ("min_edge", "unknown setting"),
    ("no_such_setting=1", "unknown setting"),
    ("override=1", "unknown setting"),
    ("max_cap=1.5", "MAX_CAP needs a whole number"),
    ("latency_ms=5", "not a single number"),
])
def test_override_refuses_what_it_cannot_set(restore, assignment, message):
    with pytest.raises(ValueError, match=message):
        config.override([assignment])


def test_settings_are_read_when_used_not_when_imported(restore, tmp_path):
    from db import database
    config.override(["start_balance=2500"])
    assert balances.Balances(database.connect(tmp_path / "t.sqlite")).amounts == {"kalshi": 2500.0, "polymarket_us": 2500.0}
    assert "min edge 0.05$" in run.trading_settings() and "start balance 2,500$" in run.trading_settings()


def test_the_command_line_rejects_a_bad_setting():
    import subprocess, sys
    from pathlib import Path
    src = Path(config.__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-m", "live.run", "--set", "min_edge=cheap"], cwd=src, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "MIN_EDGE needs a number, not 'cheap'" in done.stderr
