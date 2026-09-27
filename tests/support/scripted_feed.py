"""
A book stream that needs no venue, so a feed can run in a child process in tests.

It lives here rather than in a test file because a child process imports
it by name, from the import path pytest gives tests/support.
"""

import asyncio
from api.bookstream import BookStream
from common.timeutil import now_iso


class ScriptedStream(BookStream):
    """
    Sends a book for each contract it carries as soon as it carries it, and
    ends a gap first when its connection was lost before it started. Two
    contracts fill a connection.
    """

    name = "scripted"
    capacity = 2

    async def run(self):
        if self.down_since:
            self.on_gap(self.down_since, now_iso(), sorted(self.wanted))
            self.down_since = None
        sent = set()
        while True:
            for contract_id in sorted(self.wanted - sent):
                self.on_book(contract_id, [[0.5, 1]], [[0.6, 1]])
            sent = set(self.wanted)
            await asyncio.sleep(0.01)
