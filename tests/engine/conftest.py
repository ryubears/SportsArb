"""
A stand in for the venue streams, shared by the live tests as a fixture.
"""

import asyncio
import pytest
from api import kalshi
from engine.helper import config


@pytest.fixture(autouse=True)
def no_kalshi_key_reading(monkeypatch):
    """
    A session reads when the Kalshi key's location attestation lapses. In tests Kalshi gives no date, and nothing asks it.
    """
    monkeypatch.setattr(kalshi, "attestation_lapses", lambda: None)


class FakeStream:
    """
    A stand in for a venue BookStream that records what it was asked to do and never connects.
    """
    instances = []
    capacity = None

    def __init__(self, contract_ids, on_book, on_gap=None, log=print):
        self.wanted = set(contract_ids)
        self.on_book = on_book
        self.on_gap = on_gap
        self.added, self.removed = [], []
        FakeStream.instances.append(self)

    def room(self):
        return None if self.capacity is None else max(self.capacity - len(self.wanted), 0)

    def add(self, contract_ids):
        self.wanted |= set(contract_ids)
        self.added.append(sorted(contract_ids))

    def remove(self, contract_ids):
        self.wanted -= set(contract_ids)
        self.removed.append(sorted(contract_ids))

    async def run(self):
        await asyncio.sleep(3600)


@pytest.fixture
def fake_stream(monkeypatch):
    """
    The FakeStream class with its instance list cleared. Feeds run in the
    main process with it, since a child process could not import it.
    """
    monkeypatch.setattr(config, "FEED_PROCESSES", False)
    FakeStream.instances.clear()
    return FakeStream
