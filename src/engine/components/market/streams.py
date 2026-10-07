"""
The venue feeds that feed the recorder.

Each venue has a BookStream class in api/ that keeps one websocket
connection for a set of contracts, and a VenueFeed from feeds.py that holds
as many of them as the venue's contracts need. Streams gives each venue its
feed, in a process of its own when config.FEED_PROCESSES is set, applies
catalog changes to the running feeds, and passes their books, their gaps,
and what each venue says of its markets' trading to the recorder.
"""

import math
from api import kalshi, polymarket_us
from common.log import log
from engine.components.market.feeds import FeedProcess, VenueFeed
from engine.helper import config

STREAMS = {"kalshi": kalshi.KalshiBookStream, "polymarket_us": polymarket_us.PolymarketUSBookStream}


class Streams:
    """
    The feed of every venue. With processes, config.FEED_PROCESSES unless
    given, each venue's feed runs in a child process, and otherwise in this
    one. Either way the books and gaps go to the recorder, less the books of
    contracts removed since, which a feed may still have been sending.
    """

    def __init__(self, recorder, stream_classes=STREAMS, processes=None):
        self.recorder = recorder
        self.stream_classes = stream_classes
        self.processes = config.FEED_PROCESSES if processes is None else processes
        self.feeds = {}                                             # Venue maps to its VenueFeed, or the FeedProcess running it.
        self.wanted = {venue: set() for venue in stream_classes}   # The contracts each venue records.

    def on_book(self, venue, contract_id, bids, asks, ts, books=1, sent=None):
        """
        Pass a book to the recorder, unless its contract was removed and the feed had not caught up.
        """
        if contract_id in self.wanted[venue]:
            self.recorder.on_book(venue, contract_id, bids, asks, ts, books, sent)

    def on_state(self, venue, contract_id, why):
        """
        Pass on what a venue says of a market's trading, see BookStream.set_state(), unless its contract was removed.
        """
        if contract_id in self.wanted[venue]:
            self.recorder.on_state(venue, contract_id, why)

    def connections(self, venue):
        """
        How many connections the venue's contracts take to start with.
        """
        capacity = self.stream_classes[venue].capacity
        return max(1, math.ceil(len(self.wanted[venue]) / capacity)) if capacity else 1

    def start(self, venue, contract_ids):
        """
        Start a venue's feed for these contracts.
        """
        self.wanted[venue] = set(contract_ids)

        def on_book(contract_id, bids, asks, ts, books=1, sent=None):
            self.on_book(venue, contract_id, bids, asks, ts, books, sent)

        def on_gap(start_ts, end_ts, gap_ids):
            self.recorder.on_gap(venue, start_ts, end_ts, gap_ids)

        def on_state(contract_id, why):
            self.on_state(venue, contract_id, why)

        if self.processes:
            self.feeds[venue] = FeedProcess(venue, self.stream_classes[venue], contract_ids, config.BOOK_LEVELS, on_book, on_gap,
                                            lambda lost_ids: self.recorder.forget(venue, lost_ids), log, on_state)
        else:
            feed = self.feeds[venue] = VenueFeed(self.stream_classes[venue], on_book, on_gap, log, config.BOOK_LEVELS, on_state=on_state)
            feed.start(contract_ids)

    def update(self, targets):
        """
        Add contracts that are new and remove the ones that left the target
        list, on the running feeds. Returns a summary of the changes.
        """
        changes = []
        for venue, contract_ids in targets.items():
            new, gone = set(contract_ids) - self.wanted[venue], self.wanted[venue] - set(contract_ids)
            if not new and not gone:
                continue
            self.wanted[venue] = set(contract_ids)
            feed = self.feeds[venue]
            feed.remove(gone)
            self.recorder.forget(venue, gone)
            feed.add(new)
            changes.append(f"{venue} +{len(new)} -{len(gone)}")
        return ", ".join(changes) or "no changes"

    async def stop_all(self):
        """
        Stop every venue's feed and wait for it.
        """
        for feed in self.feeds.values():
            await feed.stop()
