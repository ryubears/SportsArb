"""
Run the live process: follow the books, scan them, trade on paper or with real money, and settle.

What runs, and where it lives in components/: market/ follows the
venues, trading/ makes the trades, and money/ keeps the cash.

- The feeds, from market/streams.py and market/feeds.py. Each venue's
  connections and books run in a child process, which passes every
  changed book on.
- The recorder, from market/record.py, holds the newest book of every
  paired contract in memory. Books are not stored.
- The scanner, from market/scan.py, prices each pair a changed book
  belongs to, stores every episode of positive edge in the opportunities
  table, and offers each episode to the desks.
- The attestation watch, from trading/notify.py, which emails a human
  before the Kalshi key's location attestation lapses.
- A Desk for each mode the run trades in, with its own money, executor,
  and settler, and its trades stored with its mode, so paper and live
  never mix. Both can run at once on the same signals, which shows how far
  the paper fills are from real ones.
  - Paper: trading/paper.py fills against the same books with the paper
    money of money/paper.py.
  - Live: trading/live.py sends real orders with the money the venues
    report through money/live.py, and emails a human through
    trading/notify.py when live trading halts.

The Session ties them together and ticks once a second. Every
CATALOG_MINUTES the catalog of each sport is refreshed in a child process,
fetch then classify then match, and the new pairs' contracts are added to
the running feeds and the closed ones removed, without reconnecting.

One run trades every sport given to --sport, comma separated, since the
money is one pool: a second process would spend the same dollars.

Run with:
    python3 -m engine.run --sport nfl
    python3 -m engine.run --sport nfl,ncaaf,mlb,nhl,nba
    python3 -m engine.run --sport nfl --seconds 120 --catalog-minutes 0
    python3 -m engine.run --sport nfl --skip-refresh
    python3 -m engine.run --sport nfl --no-scan
    python3 -m engine.run --sport nfl --no-trade
    python3 -m engine.run --sport nfl --execute live
    python3 -m engine.run --sport nfl --execute both
    python3 -m engine.run --sport nfl --set min_edge=0.03 --set min_annual_pct=50

For a long run on a laptop, stop the Mac from sleeping while it runs:
    caffeinate -i -s python3 -m engine.run --sport nfl
"""

import argparse
import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass
from catalog import fetch, pipeline
from common.log import log, with_traceback
from common.paths import ROOT
from common.processes import CONTEXT, set_up_child
from common.timeutil import now_iso
from db import database
from engine.components.market import scan
from engine.components.market.record import Recorder, load_targets
from engine.components.market.streams import Streams
from engine.components.money import settle
from engine.components.money.live import LiveBalances
from engine.components.money.paper import PaperBalances
from engine.components.trading import notify
from engine.components.trading.live import LiveExecutor
from engine.components.trading.paper import PaperExecutor
from engine.helper import config

CATALOG_MINUTES = 60    # How often the catalog is refreshed and subscriptions updated. Zero disables it.
EXECUTE = {"paper": ("paper",), "live": ("live",), "both": ("live", "paper")}     # What --execute trades in. Live first, so its orders go out first.


@dataclass(frozen=True)
class RunOptions:
    """
    What one run of the live process does, as the command line sets it.
    """
    sports: tuple = ("nfl",)                        # The sports to record and trade, from one pool of money.
    seconds: float = 0                              # Stop after this many seconds, or never when zero.
    catalog_seconds: float = CATALOG_MINUTES * 60   # Between catalog refreshes, or never when zero.
    refresh_at_start: bool = True                   # Refresh the catalog before streaming, when refreshes are on.
    scan: bool = True                               # Price the books and store episodes.
    executors: tuple = ("paper",)                   # The modes that trade the scanner's signals, 'paper' and 'live', when scanning.


def code_version():
    """
    The commit the process runs, with '-dirty' when files differ from it, or 'unknown' outside a git checkout.
    """
    try:
        return subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def book_waits():
    """
    How long both executors let a leg wait for its book to catch up with the other leg's, in words.
    """
    waits = ", ".join(f"{venue} up to {seconds}s" for venue, seconds in config.CONFIRM_SECONDS.items())
    return f"a leg waits for its book to catch up with the other's last change: {waits}"


def trading_settings():
    """
    The settings that decide what the paper trader does, in one line, so each run's log says what it ran with.
    """
    c = config
    latency = ", ".join(f"{venue} {median}ms" for venue, (median, _) in c.PAPER_LATENCY_MS.items())
    return (f"settings: min edge {c.MIN_EDGE:.2f}$, paying {c.MIN_PAYOUT_HOURS}h or more out and {c.MIN_ANNUAL_PCT}% a year, "
            f"no game once kicked off, fill share {c.FILL_SHARE}, "
            f"rejects {c.PAPER_REJECT_PROBABILITY:.0%}, latency {latency}, expected game "
            f"{', '.join(f'{sport} {hours}h' for sport, hours in c.GAME_HOURS.items())} + settle {c.SETTLE_HOURS}h, "
            f"start balance {c.PAPER_START_BALANCE:,.0f}$; {book_waits()}")


def live_settings():
    """
    The settings that decide what the live trader does, in one line.
    """
    c = config
    return (f"LIVE TRADING with real money: trades sized as paper ones, balances read every "
            f"{c.LIVE_BALANCE_SECONDS}s, email under {c.LIVE_LOW_CASH:,.2f}$ on a venue or shard; halt at {c.LIVE_UNKNOWN_LIMIT} "
            f"unknown outcomes in {c.LIVE_ORDER_WINDOW} orders, {c.LIVE_REJECT_LIMIT} refusals in a row, or a loss over "
            f"{c.LIVE_MAX_LOSS_SHARE:.0%} in {c.LIVE_RESULT_HOURS}h; {book_waits()}")


class Desk:
    """
    One mode of trading, paper or live: its executor, the money it trades,
    and the settler that pays its trades out. books is a function returning
    the recorder's newest books.
    """

    def __init__(self, mode, conn, books, notifier):
        self.mode = mode
        if mode == "paper":
            self.cash = PaperBalances(conn)
            self.executor = PaperExecutor(conn, self.cash, books, log)
        elif mode == "live":
            self.cash = LiveBalances(log)
            self.executor = LiveExecutor(conn, self.cash, books, log, notifier=notifier)
        else:
            raise ValueError(f"unknown mode {mode!r}")
        self.settler = settle.Settler(conn, self.cash, log, executor=self.executor)

    def tick(self, now, clock):
        """
        Read the live balances when due, retry exposed trades, and settle.
        """
        if self.mode == "live":
            self.cash.tick(now, clock)
        self.executor.tick(now)
        self.settler.tick(now, clock)

    def summaries(self):
        """
        One line per component about what it did since the last summary.
        """
        return [self.executor.summary(), self.settler.summary()]


class Session:
    """
    The recorder and everything that runs on its books, wired together:
    the venue connections, the scanner, and a Desk for each mode it trades
    in. Without a scanner only the books are recorded, and without a desk
    the scanner only stores what it sees.
    """

    def __init__(self, conn, sports, with_scanner=True, executors=("paper",)):
        self.conn = conn
        self.sports = sports
        self.notifier = notify.Notifier(conn, log)
        self.attestation = notify.AttestationWatch(conn, self.notifier, log)
        # The executors trade against the recorder's books, which exist once the recorder does, below.
        self.desks = [Desk(mode, conn, lambda: self.recorder.books, self.notifier) for mode in executors] if with_scanner else []
        self.scanner = scan.Scanner(conn, sports, log, [d.executor.signal for d in self.desks]) if with_scanner else None
        self.recorder = Recorder(conn, self.scanner)
        self.streams = Streams(self.recorder)
        self.last_status = self.last_summary = time.time()

    def start(self):
        """
        Open the venue connections for everything the catalog says to record.
        """
        if any(d.mode == "paper" for d in self.desks):
            log(trading_settings())
        if any(d.mode == "live" for d in self.desks):
            log(live_settings())
            if not notify.EMAIL_FILE.exists():
                log(f"no email settings in {notify.EMAIL_FILE}, alerts are only logged and stored")
        targets = load_targets(self.conn, self.sports)
        log("recording " + ", ".join(f"{len(ids)} {venue}" for venue, ids in targets.items()) + " contracts")
        if not any(targets.values()):
            log("nothing to record, run pipeline.py first")
        for venue, contract_ids in targets.items():
            self.streams.start(venue, contract_ids)
            if self.streams.connections(venue) > 1:
                log(f"{venue} needs {self.streams.connections(venue)} connections for {len(contract_ids)} contracts")

    def summaries(self):
        """
        Log one line per component about what it did since the last summary.
        """
        if self.scanner:
            log(self.scanner.summary())
        for desk in self.desks:
            for line in desk.summaries():
                log(line)

    def tick(self):
        """
        One pass of the timer: price the open episodes again, let each desk
        retry and settle, and log the status and summaries when they are due.
        """
        now = now_iso()
        if self.scanner:
            self.scanner.tick(self.recorder.books, now)
        self.attestation.tick(now, time.time())
        for desk in self.desks:
            desk.tick(now, time.time())
        if self.scanner and time.time() - self.last_summary >= config.SUMMARY_SECONDS:
            self.summaries()
            self.last_summary = time.time()
        if time.time() - self.last_status >= config.STATUS_SECONDS:
            log(self.recorder.status())
            self.last_status = time.time()

    def refreshed(self):
        """
        After a catalog refresh, apply the new targets to the connections and the scanner.
        """
        log(f"subscriptions {self.streams.update(load_targets(self.conn, self.sports))}")
        if self.scanner:
            self.scanner.reload()

    async def close(self):
        """
        Stop the connections, end the open episodes, finish the trades in
        flight and the emails being sent, and log the final summaries.
        """
        await self.streams.stop_all()
        if self.scanner:
            self.scanner.tick({}, now_iso())
        pending = [task for desk in self.desks for task in desk.executor.tasks] + list(self.notifier.tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.summaries()
        log(self.recorder.status())


def refresh_catalog(sports, log):
    """
    Refresh the catalog of each sport in turn and say what came of it, in one
    line. A sport whose refresh fails is logged and keeps the catalog it had,
    and the others refresh all the same.
    """
    results = []
    for sport in sports:
        try:
            results.append(f"{sport}: {pipeline.refresh(sport, log)}")
        except Exception as e:
            log(with_traceback(f"{sport} catalog refresh failed ({e!r}), keeping its stored catalog", e))
            results.append(f"{sport}: failed")
    return "; ".join(results)


def refresh_child(refresh, sports, answer):
    """
    Where a refresh child starts: run refresh(sports, log) and send its line back over answer.
    """
    set_up_child()
    answer.send(refresh(sports, log))


async def refresh_in_child(sports, refresh=refresh_catalog):
    """
    Run refresh(sports, log) in a child process and return its line. A
    refresh briefly holds some 300 MB, all freed when it ends, but a thread
    of this process would not give it back: the C library's malloc keeps
    what each thread frees for that thread, so each refresh that landed on
    another thread added some 50 MB for good. A child returns it all when it
    ends, and its work leaves this process's core to the books. refresh
    must be importable by name, since the child imports it.
    """
    loop = asyncio.get_running_loop()
    answers, answer = CONTEXT.Pipe(duplex=False)
    child = CONTEXT.Process(target=refresh_child, args=(refresh, sports, answer), name="catalog refresh", daemon=True)
    child.start()
    answer.close()              # The child's end, which it holds now.
    ready = loop.create_future()
    loop.add_reader(answers.fileno(), lambda: ready.done() or ready.set_result(None))
    try:
        await ready             # The child's line, or its end closing when it died without one.
        return answers.recv()
    except EOFError:
        await asyncio.to_thread(child.join)
        raise RuntimeError(f"the refresh process ended without an answer, exit code {child.exitcode}") from None
    except asyncio.CancelledError:
        child.kill()            # The run is stopping, and the next run refreshes when it starts.
        raise
    finally:
        loop.remove_reader(answers.fileno())
        answers.close()
        await asyncio.to_thread(child.join)


async def run(conn, options):
    """
    Refresh the catalog, start a Session, tick it every config.TICK_SECONDS,
    and keep the catalog fresh on a timer, as the RunOptions say, each
    refresh in a child process. A refresh that fails is logged and tried
    again at the next interval, so a bad fetch never stops the recording.
    """
    sports, seconds, catalog_seconds = options.sports, options.seconds, options.catalog_seconds
    log(f"starting {', '.join(sports)}, code {code_version()}")
    if catalog_seconds and options.refresh_at_start:
        log("refreshing catalog before starting")
        try:
            log(await refresh_in_child(sports))
        except Exception as e:
            log(with_traceback(f"catalog refresh failed ({e!r}), starting with the stored catalog", e))
    session = Session(conn, sports, options.scan, options.executors)
    session.start()
    started = last_catalog = time.time()
    refresh = None      # The background catalog refresh while one is running.
    try:
        while not seconds or time.time() - started < seconds:
            await asyncio.sleep(config.TICK_SECONDS)
            session.tick()
            if catalog_seconds and refresh is None and time.time() - last_catalog >= catalog_seconds:
                refresh = asyncio.create_task(refresh_in_child(sports))
            if refresh is not None and refresh.done():
                if refresh.exception():
                    log(with_traceback(f"catalog refresh failed ({refresh.exception()!r}), keeping current subscriptions", refresh.exception()))
                else:
                    log(f"catalog refreshed, {refresh.result()}")
                    session.refreshed()
                refresh, last_catalog = None, time.time()
    finally:
        if refresh is not None:
            refresh.cancel()
        await session.close()


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Stream the books of paired contracts, scan them, and trade.")
    ap.add_argument("--sport", default="nfl", help=f"the sports to trade, comma separated, from {', '.join(sorted(fetch.SPORTS))}")
    ap.add_argument("--seconds", type=int, default=0, help="stop after this many seconds, 0 means run forever")
    ap.add_argument("--catalog-minutes", type=int, default=CATALOG_MINUTES,
                    help="minutes between catalog refreshes, 0 means never refresh")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="start streaming at once from the stored catalog instead of refreshing first")
    ap.add_argument("--no-scan", action="store_true", help="stream the books only, without the scanner, to check the connections")
    ap.add_argument("--no-trade", action="store_true", help="scan without trading")
    ap.add_argument("--execute", choices=sorted(EXECUTE), default="paper",
                    help="trade on paper, with real money on the venues, or both at once on the same signals")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a setting from engine/helper/config.py for this run, for example --set min_edge=0.03, repeatable")
    args = ap.parse_args()
    sports = tuple(s.strip() for s in args.sport.split(",") if s.strip())
    if not sports or any(s not in fetch.SPORTS for s in sports):
        ap.error(f"--sport takes sports from {', '.join(sorted(fetch.SPORTS))}, not {args.sport!r}")
    try:
        config.override(args.set)
    except ValueError as e:
        ap.error(str(e))
    options = RunOptions(sports=sports, seconds=args.seconds, catalog_seconds=args.catalog_minutes * 60,
                         refresh_at_start=not args.skip_refresh, scan=not args.no_scan,
                         executors=() if args.no_trade else EXECUTE[args.execute])
    sys.stdout.reconfigure(line_buffering=True)     # Print immediately even when output goes to a file.
    with database.connect() as conn:
        try:
            asyncio.run(run(conn, options))
        except KeyboardInterrupt:
            print("stopped")
