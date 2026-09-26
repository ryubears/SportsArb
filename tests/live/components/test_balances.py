"""
Tests for the paper balances and the ledger behind them.
"""

import pytest
from db import database
from db.models import Ledger
from live.components import balances


def test_balances_follow_reservations_and_ledger_entries_and_survive_a_restart(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = balances.Balances(conn)
    assert cash.amounts == {"kalshi": 10000.0, "polymarket_us": 10000.0}
    cash.reserve("kalshi", 100)
    assert cash["kalshi"] == 9900
    cash.release("kalshi", 100)
    cash.book(Ledger("2026-09-20T17:30:00+00:00", "kalshi", -23.5, "buy", 1))
    cash.book(Ledger("2026-09-20T21:00:00+00:00", "polymarket_us", 50.0, "payout", 1))
    assert cash.amounts == pytest.approx({"kalshi": 9976.5, "polymarket_us": 10050.0})
    assert (cash.largest(), cash.smallest(), cash.average()) == ("polymarket_us", "kalshi", pytest.approx(10013.25))
    assert cash.summary() == "kalshi 9,976$, polymarket_us 10,050$"
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason, balance FROM ledger ORDER BY id")] == [
        ("kalshi", 10000.0, "transfer_in", 10000.0), ("polymarket_us", 10000.0, "transfer_in", 10000.0),     # Each venue's opening balance.
        ("kalshi", -23.5, "buy", 9976.5), ("polymarket_us", 50.0, "payout", 10050.0)]
    again = balances.Balances(conn)                                 # The newest entry per venue is the balance after a restart.
    assert again.amounts == pytest.approx(cash.amounts)
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 4      # And the ledger does not open again.
