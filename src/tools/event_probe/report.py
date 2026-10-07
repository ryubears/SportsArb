"""
What the event probe found, see probe.py. For each play that settled a
contract: had the venue's price already moved when the free feed showed
the play, and what was left at the old price once an order sent then
could have arrived. And how fresh the feeds were.

An order at the old price is one selling the contract's paying outcome
for CAP or less, cheap: a play can still be overturned, so a settled
contract is worth a little under 1$. The venue moved when its book first
had no cheap order after the play. LOOKBACK_SECONDS before the read is
taken as before the play, so a book with nothing cheap even then had
priced the play in long before, as for a count most expected to pass.
For MLB, whose feed times each pitch, it also shows how long after the
play itself the venue moved and the feed showed it, which is the delay a
faster feed would have to beat.

Run from src/:
    python3 -m tools.event_probe.report
    python3 -m tools.event_probe.report --cap 0.95 --since 2026-10-08
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from common.stats import quantile
from common.timeutil import utc_minute
from common.venues import SHORT_NAMES
from engine.helper import fees
from tools.event_probe.store import PATH

CAP = 0.97                  # The most an order would pay for a contract a play settled.
ORDER_SECONDS = {"kalshi": 0.012, "polymarket_us": 0.059}   # From sending an order to the venue's matching engine, medians measured 2026-10-05.
LOOKBACK_SECONDS = 120      # How long before the read the books count as before the play.
FOLLOW_SECONDS = 300        # How long after the read a cheap order is followed.
EXAMPLES = 10               # The largest chances listed.


def offers(bids, asks, winner):
    """
    The levels selling a contract's paying outcome, cheapest first: the
    asks for Yes, and for No each bid read as a No ask at 1 less its price.
    """
    return asks if winner == "yes" else [[round(1 - price, 4), size] for price, size in bids]


def cheap(levels, cap):
    return [[price, size] for price, size in levels if price <= cap + 1e-9]


def worth(levels, venue, fee_info):
    """
    The contracts on the levels that make money bought and held until they pay 1$, and the dollars they make after fees.
    """
    contracts = dollars = 0.0
    for price, size in levels:
        net = 1 - price - fees.fee_per_contract(venue, price, fee_info)
        if net > 0:
            contracts += size
            dollars += size * net
    return contracts, dollars


def follow(books, winner, read, order, cap):
    """
    What a contract's books did around a read that showed a play settle it.
    books are (ts, bids, asks) in time order, the first the book standing at
    the window's start. Returns (cheap levels at the start, cheap levels
    when an order sent at the read would arrive, when the book first had
    nothing cheap after the start or None while it still had something).
    """
    def cheap_in(book):
        return cheap(offers(book[1], book[2], winner), cap) if book else []
    standing = None
    moved = None
    for book in books:
        if book[0] <= order:
            standing = book
        if moved is None and book is not books[0] and not cheap_in(book) and cheap_in(books[0]):
            moved = book[0]
    return cheap_in(books[0] if books else None), cheap_in(standing), moved


def books_around(conn, venue, contract_id, start, end):
    """
    A contract's books from the one standing at start to end, as (ts, bids, asks) in time order.
    """
    first = conn.execute("SELECT ts, bids, asks FROM books WHERE venue = ? AND contract_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                         (venue, contract_id, start)).fetchall()
    rest = conn.execute("SELECT ts, bids, asks FROM books WHERE venue = ? AND contract_id = ? AND ts > ? AND ts <= ? ORDER BY ts",
                        (venue, contract_id, start, end)).fetchall()
    return [(ts, json.loads(bids), json.loads(asks)) for ts, bids, asks in first + rest]


def halted(conn, venue, contract_id, at):
    """
    Why the venue said the market was not trading at that time, or None while it traded.
    """
    row = conn.execute("SELECT why FROM states WHERE venue = ? AND contract_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                       (venue, contract_id, at)).fetchone()
    return row[0] if row else None


def weigh(conn, event, cap):
    """
    One settling event set against its contract's books, as a dict for the summary.
    """
    venue, contract_id, read = event["venue"], event["contract_id"], event["ts"]
    order = read + ORDER_SECONDS[venue]
    books = books_around(conn, venue, contract_id, read - LOOKBACK_SECONDS, read + FOLLOW_SECONDS)
    before, at_order, moved = follow(books, event["winner"], read, order, cap)
    contracts, dollars = worth(at_order, venue, event["fee_info"])
    if not books:
        verdict = "no book"
    elif not before:
        verdict = "priced before"
    elif moved is not None and moved <= read:
        verdict = "venue first"
    else:
        verdict = "feed first"
    stopped = halted(conn, venue, contract_id, order)
    return {**event, "verdict": verdict, "moved": moved, "halted": stopped,
            "contracts": contracts if verdict == "feed first" and not stopped else 0.0,
            "dollars": dollars if verdict == "feed first" and not stopped else 0.0}


def load_events(conn, since):
    rows = conn.execute("""
        SELECT e.sport, e.game_id, g.away, g.home, e.venue, e.contract_id, w.kind, w.subject, w.line, e.why, e.winner, e.value, e.ts,
               e.play_ended, e.play, w.fee_info
        FROM events e JOIN watched w USING (venue, contract_id) JOIN games g ON g.sport = e.sport AND g.game_id = e.game_id
        WHERE e.ts >= ? ORDER BY e.ts
    """, (since,))
    names = [d[0] for d in rows.description]
    return [{**dict(zip(names, row)), "fee_info": json.loads(row[-1]) if row[-1] else None} for row in rows]


def middle(values, digits=1):
    """
    The median and 90th percentile of values in words, or 'none'.
    """
    if not values:
        return "none"
    return f"{quantile(values, 0.5):.{digits}f}s (90%: {quantile(values, 0.9):.{digits}f}s)"


def feed_lines(conn, since):
    """
    How fresh each sport's feed was while games were under way: how long a
    read took, the cache's age, how often the content changed, and for MLB
    how long after its own stamp, and after a pitch's end, a read showed them.
    """
    lines = []
    for sport, in conn.execute("SELECT DISTINCT sport FROM reads WHERE ts >= ? ORDER BY sport", (since,)).fetchall():
        reads = conn.execute("SELECT game_id, ts, seconds, age, made, play_ended, changed FROM reads "
                             "WHERE sport = ? AND ts >= ? AND state = 'live' ORDER BY game_id, ts", (sport, since)).fetchall()
        changes, play_lags, game, last_change, last_play = [], [], None, None, None
        for game_id, ts, _, _, _, play_ended, changed in reads:
            if game_id != game:
                game, last_change, last_play = game_id, None, play_ended     # A game's first read shows plays from before it.
            if changed:
                if last_change is not None:
                    changes.append(ts - last_change)
                last_change = ts
            if play_ended is not None and play_ended != last_play:
                play_lags.append(ts - play_ended)
                last_play = play_ended
        lines.append(f"  {sport}: {len(reads):,} reads of {len({r[0] for r in reads})} games; a read took {middle([r[2] for r in reads], 2)}, "
                     f"the cache said its content was {middle([r[3] for r in reads if r[3] is not None], 0)} old, "
                     f"the content changed every {middle(changes)}")
        made = [ts - m for _, ts, _, _, m, _, _ in reads if m is not None]
        if made:
            lines.append(f"    from the feed's own stamp to our read {middle(made)}; from a pitch's end to the first read showing it {middle(play_lags)}")
    return lines


def report(conn, cap=CAP, since=0.0, examples=EXAMPLES):
    """
    The report's lines.
    """
    events = load_events(conn, since)
    lines = [f"Event probe, plays read from {utc_minute(events[0]['ts']) if events else 'nothing yet'}; cheap means {cap:.2f}$ or less",
             "", "Feeds while games were under way:", *feed_lines(conn, since), ""]
    settled = [weigh(conn, e, cap) for e in events if e["why"] != "reversed"]
    groups = defaultdict(list)
    for e in settled:
        groups[(e["sport"], e["why"], e["venue"])].append(e)
    lines.append("Plays that settled a contract, by sport, cause, and venue:")
    for (sport, why, venue), group in sorted(groups.items()):
        by = defaultdict(list)
        for e in group:
            by[e["verdict"]].append(e)
        ahead = [e["ts"] - e["moved"] for e in by["venue first"]]
        stale = [(e["moved"] or e["ts"] + FOLLOW_SECONDS) - e["ts"] for e in by["feed first"]]
        chances = [e for e in group if e["contracts"] > 0]
        lines.append(f"  {sport} {why} {SHORT_NAMES[venue]}: {len(group)} contracts; {len(by['no book'])} no book, "
                     f"{len(by['priced before'])} priced in before, {len(by['venue first'])} moved before our read (by {middle(ahead)}), "
                     f"{len(by['feed first'])} still cheap at our read (for {middle(stale)}), {sum(1 for e in group if e['halted'])} halted; "
                     f"where our order would land, {len(chances)} books still cheap with {sum(e['contracts'] for e in chances):,.0f} contracts, "
                     f"{sum(e['dollars'] for e in chances):,.2f}$ after fees")
        timed = [e for e in by["venue first"] + by["feed first"] if e["play_ended"] and e["moved"]]
        if timed:
            lines.append(f"    after the pitch itself: the venue moved {middle([e['moved'] - e['play_ended'] for e in timed])}, "
                         f"our read came {middle([e['ts'] - e['play_ended'] for e in timed])}")
    reversed_ = [e for e in events if e["why"] == "reversed"]
    lines += ["", f"Settled bets undone by a later read: {len(reversed_)}"]
    for e in reversed_:
        lines.append(f"  {stamp(e['ts'])} {e['sport']} {e['away']}@{e['home']} {SHORT_NAMES[e['venue']]} {e['contract_id']} "
                     f"now {e['winner'] or 'open'} at {e['value']}, after {e['play']}")
    best = sorted((e for e in settled if e["dollars"] > 0), key=lambda e: -e["dollars"])[:examples]
    lines += ["", f"Largest chances at our order ({len(best)}):"]
    for e in best:
        lasted = f"{e['moved'] - e['ts']:.1f}s" if e["moved"] else f"over {FOLLOW_SECONDS}s"
        lines.append(f"  {stamp(e['ts'])} {e['sport']} {e['away']}@{e['home']} {e['why']} {SHORT_NAMES[e['venue']]} {e['contract_id']} "
                     f"pays {e['winner']}: {e['contracts']:,.0f} contracts, {e['dollars']:,.2f}$, cheap for {lasted}, after {e['play']}")
    return lines


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%m-%d %H:%M:%S")


def main():
    parser = argparse.ArgumentParser(description="What the event probe found.")
    parser.add_argument("--cap", type=float, default=CAP, help="The most an order would pay for a settled contract")
    parser.add_argument("--since", help="Only plays read from this UTC date or time on, ISO 8601")
    parser.add_argument("--examples", type=int, default=EXAMPLES, help="How many of the largest chances to list")
    parser.add_argument("--db", default=str(PATH), help="The probe's records")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() if args.since else 0.0
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    print("\n".join(report(conn, args.cap, since, args.examples)))


if __name__ == "__main__":
    main()
