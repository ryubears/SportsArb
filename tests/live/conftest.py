"""
A stand in for the venue streams, shared by the live tests as a fixture.
"""

import asyncio
import pytest


class FakeStream:
    """
    A stand in for a venue BookStream that records what it was asked to do and never connects.
    """
    instances = []

    def __init__(self, contract_ids, on_book, on_gap=None, log=print):
        self.wanted = set(contract_ids)
        self.on_book = on_book
        self.on_gap = on_gap
        self.added, self.removed = [], []
        FakeStream.instances.append(self)

    def add(self, contract_ids):
        self.wanted |= set(contract_ids)
        self.added.append(sorted(contract_ids))

    def remove(self, contract_ids):
        self.wanted -= set(contract_ids)
        self.removed.append(sorted(contract_ids))

    async def run(self):
        await asyncio.sleep(3600)


@pytest.fixture
def fake_stream():
    """
    The FakeStream class with its instance list cleared.
    """
    FakeStream.instances.clear()
    return FakeStream
