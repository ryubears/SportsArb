"""
Tests for logging errors with their tracebacks.
"""

import asyncio
from common import log


def failing():
    raise ValueError("bad book")


def test_with_traceback_indents_the_traceback_under_the_message():
    try:
        failing()
    except ValueError as e:
        lines = log.with_traceback("scan failed", e).splitlines()
    assert lines[0] == "scan failed"
    assert lines[1] == "    Traceback (most recent call last):"
    assert any("in failing" in line for line in lines) and lines[-1] == "    ValueError: bad book"


def test_a_failed_background_task_is_logged_and_a_finished_or_cancelled_one_is_not():
    logs = []

    async def boom():
        failing()

    async def scenario():
        tasks = [asyncio.create_task(boom()), asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(10))]
        for task in tasks:
            task.add_done_callback(log.on_failure(logs.append, "paper trade task"))
        tasks[2].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())
    assert len(logs) == 1
    assert logs[0].startswith("paper trade task failed\n    Traceback") and logs[0].endswith("ValueError: bad book")
