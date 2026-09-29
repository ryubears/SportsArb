"""
Tests for the live balances, with the venues' answers given by the test.
"""

import asyncio
import pytest
from db.models import Ledger
from engine.components.money.live import LiveBalances

NOW = "2026-09-27T17:30:00+00:00"


def test_nothing_is_there_to_trade_before_the_first_reading_and_then_what_the_venues_say(capsys):
    logs = []
    cash = LiveBalances(logs.append, {"kalshi": lambda: 500.0, "polymarket_us": lambda: 450.25})
    assert cash.amounts == {"kalshi": 0.0, "polymarket_us": 0.0}
    asyncio.run(cash.refresh(NOW))
    assert cash.amounts == {"kalshi": 500.0, "polymarket_us": 450.25}
    assert logs == ["live balances read: kalshi 500$, polymarket_us 450$"]
    assert cash.mode == "live"


def test_reservations_and_our_own_fills_count_until_a_reading_shows_them():
    venue = {"kalshi": 500.0, "polymarket_us": 500.0}
    cash = LiveBalances(lambda m: None, {v: (lambda v=v: venue[v]) for v in venue})
    asyncio.run(cash.refresh(NOW))
    cash.reserve("kalshi", 10)
    assert cash["kalshi"] == 490
    cash.release("kalshi", 10)
    cash.apply(Ledger(NOW, "kalshi", -4.7, "buy", 1))
    cash.apply(Ledger(NOW, "polymarket_us", 2.2, "sell", 1))
    assert cash.amounts == pytest.approx({"kalshi": 495.3, "polymarket_us": 502.2})
    venue.update(kalshi=495.3, polymarket_us=502.2)          # The venues now show the fills, so the reading replaces them.
    asyncio.run(cash.refresh(NOW))
    assert cash.amounts == pytest.approx({"kalshi": 495.3, "polymarket_us": 502.2}) and cash.moved == pytest.approx({"kalshi": 0, "polymarket_us": 0})


def test_a_fill_booked_while_a_reading_runs_stays_counted_after_it():
    async def scenario():
        cash = LiveBalances(lambda m: None, {"kalshi": lambda: 500.0, "polymarket_us": lambda: 500.0})
        reading = asyncio.create_task(cash.refresh(NOW))
        await asyncio.sleep(0)                                  # The reading has asked the venues.
        cash.apply(Ledger(NOW, "kalshi", -4.7, "buy", 1))        # A fill arrives before they answer, and may not be in their answer.
        await reading
        return cash
    assert asyncio.run(scenario())["kalshi"] == pytest.approx(495.3)


def test_a_venue_that_cannot_be_read_keeps_its_last_reading():
    logs = []
    answers = {"kalshi": 500.0}

    def kalshi():
        if isinstance(answers["kalshi"], Exception):
            raise answers["kalshi"]
        return answers["kalshi"]
    cash = LiveBalances(logs.append, {"kalshi": kalshi, "polymarket_us": lambda: 300.0})
    asyncio.run(cash.refresh(NOW))
    answers["kalshi"] = TimeoutError("timed out")
    asyncio.run(cash.refresh("2026-09-27T17:30:30+00:00"))
    assert cash.amounts == {"kalshi": 500.0, "polymarket_us": 300.0}
    assert cash.read_at == {"kalshi": NOW, "polymarket_us": "2026-09-27T17:30:30+00:00"}
    assert logs[-1].startswith("live balance of kalshi could not be read (TimeoutError('timed out')), keeping the last\n    Traceback")
    asyncio.run(cash.refresh("2026-09-27T17:31:00+00:00"))            # The same error again is one line.
    assert logs[-1] == "live balance of kalshi could not be read (TimeoutError('timed out')), keeping the last"


def test_a_payout_counts_as_live_money_at_once_but_is_spent_only_once_a_reading_shows_it():
    venue = {"kalshi": 500.0, "polymarket_us": 500.0}
    cash = LiveBalances(lambda m: None, {v: (lambda v=v: venue[v]) for v in venue})
    asyncio.run(cash.refresh(NOW))
    cash.apply(Ledger(NOW, "kalshi", 10.0, "payout", 1))
    assert cash["kalshi"] == 500.0 and cash.total() == 1010.0
    venue["kalshi"] = 510.0                                             # The venue has paid it.
    asyncio.run(cash.refresh(NOW))
    assert cash["kalshi"] == 510.0 and cash.total() == 1010.0 and cash.paid["kalshi"] == 0


def test_readings_are_taken_on_a_timer_and_at_once_after_a_payout():
    reads = []

    async def scenario():
        cash = LiveBalances(lambda m: None, {"kalshi": lambda: reads.append(1) or 500.0, "polymarket_us": lambda: 500.0})
        cash.tick(NOW, 1000.0)                                  # The first reading is due at once.
        await cash.readings.running
        cash.tick(NOW, 1010.0)                                  # Not due again yet.
        cash.apply(Ledger(NOW, "kalshi", 5.0, "payout", 1))      # The venue pays on its own, so it is read again at once.
        assert cash["kalshi"] == 500.0
        cash.tick(NOW, 1011.0)
        await cash.readings.running
        cash.tick(NOW, 1011.0 + 30)
        await cash.readings.running
    asyncio.run(scenario())
    assert len(reads) == 3


def test_an_order_on_a_kalshi_shard_can_spend_only_what_that_shard_has_free():
    shards = {0: 80.0, 3: 20.0}
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: sum(shards.values()), "polymarket_us": lambda: 100.0},
                        {"kalshi": lambda: dict(shards)})
    asyncio.run(cash.refresh(NOW))
    assert (cash.available("kalshi"), cash.available("kalshi", 0), cash.available("kalshi", 3)) == (100.0, 80.0, 20.0)
    assert cash.available("kalshi", 2) == 0.0 and cash.available("polymarket_us", 3) == 100.0    # A shard not listed, a venue without shards.
    cash.reserve("kalshi", 15.0, 3)
    cash.apply(Ledger(NOW, "kalshi", -10.0, "buy", 1), 0)
    assert (cash.available("kalshi", 0), cash.available("kalshi", 3), cash["kalshi"]) == (70.0, 5.0, 75.0)
    assert cash.spendable("kalshi", 3) == 0.0                                               # Under the floor on that shard.
    shards.update({0: 70.0})                                                               # The venue shows the buy.
    cash.release("kalshi", 15.0, 3)
    asyncio.run(cash.refresh(NOW))
    assert (cash.available("kalshi", 0), cash.available("kalshi", 3)) == (70.0, 20.0)       # Counted once, not twice.
