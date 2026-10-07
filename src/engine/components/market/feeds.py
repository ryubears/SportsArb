"""
One venue's feed: its connections, run here or in a process of its own.

A VenueFeed holds a venue's BookStreams, as many as the venue's limits
need, and applies changes to the wanted contracts on the live connections
in place, so a catalog refresh never reconnects.

A Python process runs on one core, and with nine games at once the feeds
alone kept it busy: every message is received over TLS, parsed, and
applied to its book before anything is priced. So each venue's feed can
run in a child process, as a FeedProcess, which keeps the books and passes
on each changed book's best levels, and each gap, over a pipe. The main
process keeps the recorder, the scanner, and the desks, since pricing
needs every venue's books in one place.

A child never waits on the main process. Its books wait in an Outbox, where
a newer book of a contract replaces an older one not yet sent, and a thread
of the child's sends the outbox whenever the pipe takes more. A main
process that falls behind gets the newest books rather than a backlog.
What the venue says of a market's trading, see BookStream.set_state(),
goes the same way, in order, ahead of the books.

A child that dies is started again, after a pause that grows while it keeps
dying. Its books are dropped until the new connections send them, and the
stretch between is stored as a gap.
"""

import asyncio
import threading
from common.log import log, with_traceback
from common.processes import CONTEXT, set_up_child
from common.timeutil import now_iso

RESTART_SECONDS = (1, 5, 30)    # Pause before starting a dead feed again, longer while it keeps dying. The last value repeats.
STOP_SECONDS = 5                # How long a child may take to stop before it is killed.


class VenueFeed:
    """
    One venue's connections. A venue whose stream class sets a capacity gets
    as many connections as its contracts need: new contracts go to the
    connections in order, as many as each one's room() allows, and to new
    connections once none has room. Contracts a venue refuses on one
    connection come back through its on_refused and are placed the same
    way. Each book is passed to on_book(contract_id, bids, asks, ts, sent=)
    with the time it arrived and the venue's time for it, as the stream
    gives it, depth levels a side, each gap to
    on_gap(start_ts, end_ts, contract_ids), and what the venue says of a
    market's trading to on_state(contract_id, why), see
    BookStream.set_state(). down_since, when given, is when the venue's
    connections were lost before these, so their first subscriptions end a
    gap.
    """

    def __init__(self, stream_class, on_book, on_gap, log=log, depth=None, down_since=None, on_state=None):
        self.stream_class = stream_class
        self.on_book = on_book
        self.on_gap = on_gap
        self.on_state = on_state or (lambda contract_id, why: None)
        self.log = log
        self.depth = depth
        self.down_since = down_since
        self.streams = []
        self.tasks = []

    @property
    def capacity(self):
        return self.stream_class.capacity

    def open(self, contract_ids):
        """
        Open one more connection, carrying these contracts.
        """
        stream = self.stream_class(list(contract_ids), lambda cid, bids, asks, sent=None: self.on_book(cid, bids, asks, now_iso(), sent=sent),
                                   self.on_gap, self.log)
        if self.depth:
            stream.depth = self.depth
        stream.down_since = self.down_since
        stream.on_refused = self.add
        stream.on_state = self.on_state
        self.streams.append(stream)
        self.tasks.append(asyncio.create_task(stream.run()))
        return stream

    def start(self, contract_ids):
        """
        Open the connections for these contracts, one per capacity when there is one.
        """
        ids = sorted(contract_ids)
        size = self.capacity or max(len(ids), 1)
        for i in range(0, max(len(ids), 1), size):
            self.open(ids[i:i + size])
        self.down_since = None          # Connections opened later are new ones, not ones that were lost.

    def add(self, contract_ids):
        """
        Add contracts to the connections with room, opening new ones when they are full.
        """
        ids = sorted(contract_ids)
        for stream in self.streams:
            if not ids:
                return
            room = stream.room()
            taken = ids if room is None else ids[:room]
            if taken:
                stream.add(taken)
                ids = ids[len(taken):]
        while ids:
            size = self.capacity or len(ids)
            self.open(ids[:size])
            ids = ids[size:]

    def remove(self, contract_ids):
        """
        Remove contracts from whichever connections carry them.
        """
        gone = set(contract_ids)
        for stream in self.streams:
            stream.remove(gone & stream.wanted)

    async def stop(self):
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


class Outbox:
    """
    What a feed child has for the main process, sent over conn by a thread
    of its own, from start(), so the feed never waits on the pipe. A book
    waits keyed by its contract, a newer one replacing an older one not yet
    sent, with the count of books behind it. A gap waits in order, and drops
    its contracts' books not yet sent, which it clears anyway. What the
    venue says of a market's trading waits in order with the gaps.
    """

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.books = {}                     # Contract id maps to (bids, asks, ts, sent, books behind it) not yet sent.
        self.gaps = []                      # ("gap", start_ts, end_ts, contract_ids) and ("state", contract_id, why) not yet
                                            # sent, in order.
        self.pending = threading.Event()    # Set while something waits to be sent.
        self.closed = False
        self.thread = None

    def book(self, contract_id, bids, asks, ts, sent=None):
        with self.lock:
            unsent = self.books.get(contract_id)
            self.books[contract_id] = (bids, asks, ts, sent, unsent[4] + 1 if unsent else 1)
        self.pending.set()

    def gap(self, start_ts, end_ts, contract_ids):
        with self.lock:
            for contract_id in contract_ids:
                self.books.pop(contract_id, None)
            self.gaps.append(("gap", start_ts, end_ts, contract_ids))
        self.pending.set()

    def state(self, contract_id, why):
        with self.lock:
            self.gaps.append(("state", contract_id, why))
        self.pending.set()

    def take(self):
        """
        Everything waiting, as the messages to send in order: the gaps and states, then one message with every book.
        """
        with self.lock:
            self.pending.clear()
            messages, self.gaps = self.gaps, []
            if self.books:
                messages.append(("books", [(contract_id, *book) for contract_id, book in self.books.items()]))
                self.books = {}
        return messages

    def send(self):
        """
        Send what waits whenever something does, until closed with nothing left, or until the main process is gone.
        """
        while True:
            self.pending.wait()
            for message in self.take():
                try:
                    self.conn.send(message)
                except (OSError, ValueError):
                    return          # The main process is gone.
            if self.closed and not self.pending.is_set():
                return

    def start(self):
        self.thread = threading.Thread(target=self.send, name="outbox", daemon=True)
        self.thread.start()

    def close(self):
        """
        Send what is left and stop the thread.
        """
        self.closed = True
        self.pending.set()
        self.thread.join()


async def run_child(stream_class, contract_ids, depth, down_since, commands, updates):
    """
    A feed child's loop: the venue's connections feeding the outbox, and the
    main process's commands, ('add', contract_ids), ('remove', contract_ids),
    and ('stop', None), until told to stop or the main process is gone.
    """
    outbox = Outbox(updates)
    outbox.start()
    feed = VenueFeed(stream_class, outbox.book, outbox.gap, log, depth, down_since, outbox.state)
    feed.start(contract_ids)
    stop = asyncio.Event()

    def on_command():
        try:
            while commands.poll():
                action, ids = commands.recv()
                if action == "add":
                    feed.add(ids)
                elif action == "remove":
                    feed.remove(ids)
                else:
                    stop.set()
        except (EOFError, OSError):
            stop.set()      # The main process is gone.

    loop = asyncio.get_running_loop()
    loop.add_reader(commands.fileno(), on_command)
    await stop.wait()
    loop.remove_reader(commands.fileno())
    await feed.stop()
    outbox.close()


def child_main(stream_class, contract_ids, depth, down_since, commands, updates):
    """
    Where a feed child starts.
    """
    set_up_child()
    asyncio.run(run_child(stream_class, contract_ids, depth, down_since, commands, updates))


class FeedProcess:
    """
    One venue's VenueFeed, run in a child process. What the child sends is
    passed on here in the main process: each book to on_book(contract_id,
    bids, asks, ts, books, sent) with the number of books it stands for and
    the venue's time for the newest, each gap to on_gap(start_ts, end_ts,
    contract_ids), and what the venue says of a market's trading to
    on_state(contract_id, why). When the child dies, on_lost(contract_ids)
    drops its books, and what its venue had said of its markets, which a
    new child hears again only as it changes, and a new child starts after
    a pause, its first subscriptions ending a gap from when the old one was
    found dead. Must be made inside the running loop.
    """

    def __init__(self, venue, stream_class, contract_ids, depth, on_book, on_gap, on_lost, log=log, on_state=None):
        self.venue = venue
        self.stream_class = stream_class
        self.wanted = set(contract_ids)
        self.depth = depth
        self.on_book = on_book
        self.on_gap = on_gap
        self.on_state = on_state or (lambda contract_id, why: None)
        self.on_lost = on_lost
        self.log = log
        self.loop = asyncio.get_running_loop()
        self.process = self.commands = self.updates = None
        self.deaths = 0             # Deaths in a row, which lengthen the pause before a restart. A child sending books resets it.
        self.restarting = None      # The timer that starts the next child while one is due.
        self.stopping = False
        self.spawn(None)

    def lost(self):
        """
        The child is gone. Stop listening, and unless stopping, drop its books and start another after a pause.
        """
        self.loop.remove_reader(self.updates.fileno())
        self.updates.close()
        self.commands.close()
        if self.stopping:
            return
        lost_at = now_iso()
        if self.process.is_alive():
            self.process.kill()         # Its pipe closed, so it can no longer feed us whatever it is doing.
        pause = RESTART_SECONDS[min(self.deaths, len(RESTART_SECONDS) - 1)]
        self.deaths += 1
        self.log(f"{self.venue} feed process {self.process.pid} ended, starting another in {pause}s")
        self.on_lost(sorted(self.wanted))
        self.restarting = self.loop.call_later(pause, self.spawn, lost_at)

    def receive(self):
        """
        Pass on one message from the child, or find it gone when its pipe
        closes. The loop calls again while more wait, so the session's other
        work runs between messages however fast the child sends.
        """
        try:
            message = self.updates.recv()
        except (EOFError, OSError):
            self.lost()
            return
        try:
            if message[0] == "books":
                self.deaths = 0
                for contract_id, bids, asks, ts, sent, books in message[1]:
                    self.on_book(contract_id, bids, asks, ts, books, sent)
            elif message[0] == "state":
                self.on_state(*message[1:])
            else:
                self.on_gap(*message[1:])
        except Exception as e:
            self.log(with_traceback(f"{self.venue} feed message failed ({e!r})", e))

    def spawn(self, down_since):
        """
        Start a child for the wanted contracts and listen for what it sends.
        """
        self.restarting = None
        commands, self.commands = CONTEXT.Pipe(duplex=False)
        self.updates, updates = CONTEXT.Pipe(duplex=False)
        self.process = CONTEXT.Process(target=child_main, name=f"{self.venue} feed", daemon=True,
                                       args=(self.stream_class, sorted(self.wanted), self.depth, down_since, commands, updates))
        self.process.start()
        commands.close()            # The child's ends, which it holds now.
        updates.close()
        self.loop.add_reader(self.updates.fileno(), self.receive)
        self.log(f"{self.venue} feed runs in process {self.process.pid}")

    def send(self, command):
        """
        Send a command to the child. One that finds the child gone is dropped, since the next child starts from the wanted contracts.
        """
        try:
            self.commands.send(command)
        except (OSError, ValueError):
            pass

    def add(self, contract_ids):
        if contract_ids:
            self.wanted |= set(contract_ids)
            self.send(("add", sorted(contract_ids)))

    def remove(self, contract_ids):
        if contract_ids:
            self.wanted -= set(contract_ids)
            self.send(("remove", sorted(contract_ids)))

    async def stop(self):
        """
        Tell the child to stop and wait for it, killing it when it takes longer than STOP_SECONDS.
        """
        self.stopping = True
        if self.restarting:
            self.restarting.cancel()
        self.send(("stop", None))
        await asyncio.to_thread(self.process.join, STOP_SECONDS)
        if self.process.is_alive():
            self.process.kill()
            await asyncio.to_thread(self.process.join)
        while not self.updates.closed:
            self.receive()              # What it sent before it stopped, then its pipe closing.
