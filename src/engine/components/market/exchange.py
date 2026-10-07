"""
Whether each venue's exchange is taking orders, as the venue says.

Kalshi says whether it trades, shard by shard, through a public status
call, which is read every config.EXCHANGE_STATUS_SECONDS in a background
thread. It stops every Thursday from 3 to 5 AM Eastern, which
common/venues.py knows ahead, and at any other time it finds an issue,
which only this call tells. While a venue, or the shard a market trades
on, has stopped, no trade opens with a leg there and nothing held there is
sold back, see Executor.trading(). Polymarket US publishes no such status;
its markets say themselves when they are not open, see market/record.py.
A reading that fails keeps what the last one said.
"""

import asyncio
from api import kalshi
from common.periodic import Periodic
from engine.helper import config

READERS = {"kalshi": kalshi.exchange_trading}   # How each venue that publishes it says whether it trades, as {shard or None: bool}.


class ExchangeStatus:
    """
    The venues' word on whether they trade, read when due from the session's tick. readers maps a venue to a function
    returning whether it trades on each shard, or on all of them under None, by default READERS.
    """

    def __init__(self, log=print, readers=None):
        self.log = log
        self.readers = READERS if readers is None else readers
        self.stopped = set()        # (venue, shard or None) not trading as last read, None for the whole venue.
        self.failing = set()        # Venues whose last reading failed, so a failure is logged once until one succeeds.
        self.readings = Periodic(lambda: config.EXCHANGE_STATUS_SECONDS, log, "exchange status reading")

    def trading(self, venue, shard=None):
        """
        Whether the venue takes orders on the shard, None for a venue without shards, as last read.
        """
        return (venue, None) not in self.stopped and (venue, shard) not in self.stopped

    def note(self, venue, shards):
        """
        Keep what one venue said, {shard or None: trading}, and log each shard that stopped or trades again. A reading
        shard by shard says the whole venue is not stopped.
        """
        shards = shards if None in shards else {None: True, **shards}
        for shard, trading in sorted(shards.items(), key=lambda item: (item[0] is not None, item[0] or 0)):
            key = (venue, shard)
            where = venue if shard is None else f"{venue} shard {shard}"
            if not trading and key not in self.stopped:
                self.stopped.add(key)
                self.log(f"{where} says it is not trading: no trade opens there, nor is anything sold back, until it does")
            elif trading and key in self.stopped:
                self.stopped.discard(key)
                self.log(f"{where} says it is trading again")

    async def read(self):
        """
        Read every venue's status, each in a thread, keeping the last reading of one that fails.
        """
        for venue, reader in self.readers.items():
            try:
                shards = await asyncio.to_thread(reader)
            except Exception as e:
                if venue not in self.failing:
                    self.failing.add(venue)
                    self.log(f"{venue} exchange status could not be read ({e!r}), keeping the last reading")
                continue
            self.failing.discard(venue)
            self.note(venue, shards)

    def tick(self, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a reading when one is due and none runs.
        """
        self.readings.tick(clock, self.read)
