"""
The venue connections that feed the recorder.

Each venue has a BookStream class in api/ that keeps one websocket
connection for a set of contracts. Streams holds those connections for
every venue, opens as many as a venue's capacity needs, and applies
changes to the wanted contracts on the live connections in place, so a
catalog refresh never reconnects. Book updates and gaps go to the recorder.
"""

import asyncio
from api import kalshi, polymarket_us
from common.log import log

STREAMS = {"kalshi": kalshi.KalshiBookStream, "polymarket_us": polymarket_us.PolymarketUSBookStream}


class Streams:
    """
    The BookStreams for every venue, each on its own long lived connection.
    A venue whose stream class sets a capacity gets as many connections as
    its contracts need, filled in order. Changes to the wanted contracts are
    applied to the live connections in place.
    """

    def __init__(self, recorder, stream_classes=STREAMS):
        self.recorder = recorder
        self.stream_classes = stream_classes
        self.streams = {venue: [] for venue in stream_classes}
        self.tasks = []

    def on_book(self, venue, contract_id, bids, asks):
        """
        Pass a book update to the recorder, unless the contract was removed and the feed has not caught up.
        """
        if any(contract_id in stream.wanted for stream in self.streams[venue]):
            self.recorder.on_book(venue, contract_id, bids, asks)

    def capacity(self, venue):
        return getattr(self.stream_classes[venue], "capacity", None)

    def open(self, venue, contract_ids):
        """
        Open one more connection for a venue, carrying these contracts.
        """
        stream = self.stream_classes[venue](list(contract_ids), lambda cid, b, a: self.on_book(venue, cid, b, a),
                                            lambda start_ts, end_ts: self.recorder.on_gap(venue, start_ts, end_ts), log)
        self.streams[venue].append(stream)
        self.tasks.append(asyncio.create_task(stream.run()))
        return stream

    def start(self, venue, contract_ids):
        """
        Open a venue's connections for these contracts, one per capacity when it has one.
        """
        ids = sorted(contract_ids)
        size = self.capacity(venue) or max(len(ids), 1)
        for i in range(0, max(len(ids), 1), size):
            self.open(venue, ids[i:i + size])

    def add(self, venue, contract_ids):
        """
        Add contracts to the venue's connections with room, opening new ones when they are full.
        """
        ids = sorted(contract_ids)
        capacity = self.capacity(venue)
        for stream in self.streams[venue]:
            room = len(ids) if capacity is None else max(capacity - len(stream.wanted), 0)
            if room:
                stream.add(ids[:room])
                ids = ids[room:]
            if not ids:
                return
        while ids:
            size = capacity or len(ids)
            self.open(venue, ids[:size])
            ids = ids[size:]

    def update(self, targets):
        """
        Add contracts that are new and remove the ones that left the target
        list, on the live connections. Returns a summary of the changes.
        """
        changes = []
        for venue, contract_ids in targets.items():
            wanted = set().union(*(stream.wanted for stream in self.streams[venue]))
            new, gone = set(contract_ids) - wanted, wanted - set(contract_ids)
            for stream in self.streams[venue]:
                stream.remove(gone & stream.wanted)
            self.recorder.forget(venue, gone)
            self.add(venue, new)
            if new or gone:
                changes.append(f"{venue} +{len(new)} -{len(gone)}")
        return ", ".join(changes) or "no changes"

    async def stop_all(self):
        """
        Cancel every connection and wait for them to finish.
        """
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks = []
