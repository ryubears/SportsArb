"""
What the trading tests share.
"""

import pytest
from engine.components.trading import brakes


@pytest.fixture(autouse=True)
def halt_file(tmp_path, monkeypatch):
    """
    The live halt file in the test's own folder, so no test touches data/live_halt.txt.
    """
    monkeypatch.setattr(brakes, "HALT_FILE", tmp_path / "live_halt.txt")
    return brakes.HALT_FILE
