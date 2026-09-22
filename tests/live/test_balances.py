"""
Tests for the paper balances and the ledger behind them.
"""

import pytest
from db import database
from db.models import Ledger
from live import balances


def test_balances_follow_reservations_and_ledger_entries_and_survive_a_restart(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    assert cash.amounts == {"kalshi": 5000.0, "polymarket_us": 5000.0}
    cash.reserve("kalshi", 100)
    assert cash["kalshi"] == 4900
    cash.release("kalshi", 100)
    cash.book(Ledger("2026-09-20T17:30:00+00:00", "kalshi", -23.5, "buy", 1))
    cash.book(Ledger("2026-09-20T21:00:00+00:00", "polymarket_us", 50.0, "payout", 1))
    assert cash.amounts == pytest.approx({"kalshi": 4976.5, "polymarket_us": 5050.0})
    assert (cash.richest(), cash.poorest(), cash.average()) == ("polymarket_us", "kalshi", pytest.approx(5013.25))
    assert cash.words() == "kalshi 4,976$, polymarket_us 5,050$"
    again = balances.Balances(conn)                                 # Only the ledger survives a restart, and that is enough.
    assert again.amounts == pytest.approx(cash.amounts)
