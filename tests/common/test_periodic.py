"""
Tests for work started on a timer.
"""

import asyncio
from common.periodic import Periodic


def test_work_starts_at_once_then_on_its_timer_and_never_twice_at_a_time():
    starts, logs = [], []
    every = Periodic(lambda: 30, logs.append, "the check")

    async def work(n):
        starts.append(n)
        await asyncio.sleep(0)

    async def scenario():
        every.tick(1000.0, lambda: work(1))             # The first start is at once.
        every.tick(1040.0, lambda: work(2))             # Due, but the first still runs.
        await every.running
        every.tick(1041.0, lambda: work(3))             # Due since 1030, and now free.
        await every.running
        every.tick(1050.0, lambda: work(4))             # Too soon.
        every.again()
        every.tick(1050.0, lambda: work(5))             # Asked to start again at once.
        await every.running
    asyncio.run(scenario())
    assert starts == [1, 3, 5] and logs == []


def test_a_failure_is_logged_and_the_next_start_comes_on_time():
    logs = []
    every = Periodic(lambda: 30, logs.append, "the check")

    async def broken():
        raise RuntimeError("venue down")

    async def scenario():
        every.tick(1000.0, broken)
        await asyncio.gather(every.running, return_exceptions=True)
        await asyncio.sleep(0)                          # Let the done callback log it.
        every.tick(1030.0, broken)
        await asyncio.gather(every.running, return_exceptions=True)
        await asyncio.sleep(0)
    asyncio.run(scenario())
    assert len(logs) == 2 and logs[0].startswith("the check failed")
