"""
Tests for the venues' word on whether their exchanges trade.
"""

import asyncio
from engine.components.market import exchange


def test_a_shard_that_stops_is_not_traded_until_it_trades_again_and_each_change_is_logged():
    said = [{0: True, 3: False}, {0: True, 3: False}, {None: False}, {0: True, 3: True}]
    logs = []
    status = exchange.ExchangeStatus(logs.append, {"kalshi": lambda: said.pop(0)})
    asyncio.run(status.read())
    assert status.trading("kalshi", 0) and not status.trading("kalshi", 3) and status.trading("polymarket_us")
    asyncio.run(status.read())                          # Unchanged, so nothing more is logged.
    asyncio.run(status.read())                          # The whole exchange stops.
    assert not status.trading("kalshi", 0) and not status.trading("kalshi", None)
    asyncio.run(status.read())                          # A shard by shard answer says the whole exchange trades again.
    assert status.trading("kalshi", 0) and status.trading("kalshi", 3)
    assert logs == ["kalshi shard 3 says it is not trading: no trade opens there, nor is anything sold back, until it does",
                    "kalshi says it is not trading: no trade opens there, nor is anything sold back, until it does",
                    "kalshi says it is trading again", "kalshi shard 3 says it is trading again"]


def test_a_reading_that_fails_keeps_the_last_and_is_logged_once_until_one_succeeds():
    answers = [{3: False}, RuntimeError("down"), RuntimeError("down"), {3: True}, RuntimeError("down")]
    logs = []

    def read():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    status = exchange.ExchangeStatus(logs.append, {"kalshi": read})
    for _ in range(3):
        asyncio.run(status.read())
    assert not status.trading("kalshi", 3)
    asyncio.run(status.read())
    asyncio.run(status.read())
    assert status.trading("kalshi", 3)
    assert [line.split(" (")[0] for line in logs] == [
        "kalshi shard 3 says it is not trading: no trade opens there, nor is anything sold back, until it does",
        "kalshi exchange status could not be read", "kalshi shard 3 says it is trading again", "kalshi exchange status could not be read"]
