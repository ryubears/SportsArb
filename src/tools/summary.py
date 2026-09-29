"""
Print a summary of everything in the database.

Row counts and time ranges for each table, pairs by sport and kind, and
for the recent window the feed drops, the opportunities found, and the
trades made, paper and live apart. Reads only, so it is safe to run
while the live process is writing.

This script opens the database file directly rather than importing the
db package, so it runs from any folder without setting an import path.
The one exception is the live money: live balances are not in the
database but on the venues, so they are read from each venue with the
same read-only calls the live process makes, using the keys in data/.
--no-live leaves that out, and a venue that cannot be read says why.

Run with:
    python3 src/tools/summary.py
    python3 src/tools/summary.py --hours 6
    python3 src/tools/summary.py --no-live
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sportsarb.sqlite"
SRC = Path(__file__).resolve().parent.parent       # Where the venue clients are, for the live balances.


def query_rows(conn, sql, params=()):
    """
    Run a query and return the rows as tuples.
    """
    return [tuple(r) for r in conn.execute(sql, params)]


def first_value(conn, sql, params=()):
    """
    Run a query and return the first column of the first row.
    """
    return conn.execute(sql, params).fetchone()[0]


def short_time(ts):
    """
    An ISO timestamp cut to date and seconds, or a dash when missing.
    """
    return ts[:19].replace("T", " ") if ts else "-"


def print_table(title, header, body):
    """
    Print a small aligned table with a title line.
    """
    print(f"\n{title}")
    widths = [max(len(str(r[i])) for r in [header] + body) for i in range(len(header))]
    for r in [header] + body:
        print("  " + "  ".join(str(v).ljust(w) if i == 0 else str(v).rjust(w) for i, (v, w) in enumerate(zip(r, widths))))


# SECTIONS

def print_storage(conn):
    size = os.path.getsize(DB_PATH)
    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    if wal.exists():
        size += os.path.getsize(wal)
    print(f"database {DB_PATH}")
    print(f"size {size / 1e6:,.0f} MB")
    # Listed in pipeline order rather than alphabetically.
    tables = ["contracts", "bets", "pairs", "gaps", "opportunities", "trades", "settlements", "orders", "ledger", "alerts", "transfers"]
    print_table("tables", ("table", "rows"), [(t, f"{first_value(conn, f'SELECT COUNT(*) FROM {t}'):,}") for t in tables])


def print_contracts(conn, now):
    body = query_rows(conn, """
        SELECT venue, sport, COUNT(*), SUM(close_time IS NULL OR close_time > ?), MAX(last_seen)
        FROM contracts GROUP BY venue, sport ORDER BY venue, sport""", (now,))
    print_table("contracts", ("venue", "sport", "total", "open", "last fetched"),
                [(v, s, f"{n:,}", f"{o:,}", short_time(t)) for v, s, n, o, t in body])


def print_pairs(conn):
    body = query_rows(conn, """
        SELECT sport, kind, COUNT(*), SUM(contracts), SUM(game_date IS NOT NULL)
        FROM pairs WHERE id IN (SELECT pair_id FROM bets WHERE pair_id IS NOT NULL) GROUP BY sport, kind ORDER BY sport, kind""")
    print_table("pairs", ("sport", "kind", "pairs", "contracts", "games"), body)
    print(f"  total {first_value(conn, 'SELECT COUNT(*) FROM pairs WHERE id IN (SELECT pair_id FROM bets WHERE pair_id IS NOT NULL)'):,}, "
          f"last matched {short_time(first_value(conn, 'SELECT MAX(matched_at) FROM pairs'))}")


def print_gaps(conn, since, hours):
    """
    How often each venue's feed dropped in the window, and for how long.
    """
    gaps = query_rows(conn, """
        SELECT venue, COUNT(*), COALESCE(SUM((julianday(end_ts) - julianday(start_ts)) * 86400), 0), SUM(end_ts IS NULL)
        FROM gaps WHERE start_ts >= ? GROUP BY venue ORDER BY venue""", (since,))
    print(f"\nfeed drops, last {hours} hours: " + (", ".join(
        f"{v} {n} ({secs:.0f}s down{f', {open_} without an end' if open_ else ''})" for v, n, secs, open_ in gaps) if gaps else "none"))


def print_opportunities(conn, since, hours):
    total = first_value(conn, "SELECT COUNT(*) FROM opportunities")
    recent = first_value(conn, "SELECT COUNT(*) FROM opportunities WHERE start_ts >= ?", (since,))
    if not total:
        print("\nopportunities: none yet, the live process's scanner writes them")
        return
    # Asked apart, so the first start comes from the index. Episodes are stored as they end, so the newest one ended last.
    first = first_value(conn, "SELECT MIN(start_ts) FROM opportunities")
    last = first_value(conn, "SELECT end_ts FROM opportunities ORDER BY id DESC LIMIT 1")
    print(f"\nopportunities {total:,} episodes in all, covering {short_time(first)} to {short_time(last)} UTC, "
          f"{recent:,} in the last {hours} hours")
    if not recent:
        return
    # Capital required is the fillable size times the cost of both legs and fees, which is one dollar minus the edge.
    body = query_rows(conn, """
        SELECT p.sport, p.kind, COUNT(*), SUM(live), ROUND(100 * MAX(peak_edge), 1), ROUND(MAX(peak_profit), 2),
               ROUND(MAX(peak_size * (1 - peak_edge))), ROUND(MAX(return_pct), 2), ROUND(MAX(annual_pct)),
               ROUND(AVG(days_held), 1), SUM(annual_pct >= 10)
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id WHERE start_ts >= ? GROUP BY p.sport, p.kind ORDER BY p.sport, p.kind""", (since,))
    print_table(f"by kind, last {hours} hours", ("sport", "kind", "episodes", "live", "best edge c", "best profit $", "max capital $",
                            "best return %", "best annual %", "avg days held", "beat 10%/yr"), body)
    best = query_rows(conn, """
        SELECT p.label, trade, ROUND(100 * peak_edge, 1), ROUND(peak_size), ROUND(peak_size * (1 - peak_edge)),
               ROUND(peak_profit, 2), ROUND(return_pct, 2), ROUND(annual_pct), ROUND(days_held, 1), ROUND(seconds), live
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id WHERE start_ts >= ? AND annual_pct >= 10 ORDER BY peak_profit DESC LIMIT 8""", (since,))
    print_table(f"largest that beat the target, last {hours} hours",
                ("bet", "trade", "edge c", "size", "capital $", "profit $", "return %", "annual %", "days held", "seconds", "live"),
                [(l[:40], t, e, f"{s:,.0f}", f"{cap:,.0f}", p, r, f"{a:,.0f}", d, f"{sec:,.0f}", "yes" if lv else "no")
                 for l, t, e, s, cap, p, r, a, d, sec, lv in best])


def print_paper_money(conn):
    """
    The paper balances from the ledger and the paper transfers. Live money is on the venues, see live_check.py.
    """
    balances = query_rows(conn, """
        SELECT l.venue, ROUND(l.balance, 2), ROUND(COALESCE((SELECT SUM(amount) FROM transfers WHERE to_venue = l.venue AND arrived_at IS NULL), 0))
        FROM ledger l WHERE l.id IN (SELECT MAX(id) FROM ledger GROUP BY venue) ORDER BY l.venue""")
    if balances:
        print("  paper balances from the ledger: " + ", ".join(f"{v} {a:,.2f}$" + (f" (+{p:,.0f}$ pending)" if p else "") for v, a, p in balances))
    transfers = query_rows(conn, "SELECT from_venue, to_venue, ROUND(amount), reason, substr(requested_at, 1, 10), substr(expected_at, 1, 10), substr(arrived_at, 1, 10) FROM transfers ORDER BY id DESC LIMIT 5")
    if transfers:
        print_table("paper transfers", ("from", "to", "amount $", "reason", "requested", "status"),
                    [(f, t, a, r, q, f"arrived {v}" if v else f"in transit, due {e}") for f, t, a, r, q, e, v in transfers])


def print_live_orders(conn, since, hours):
    """
    The real orders sent in the window, by venue, purpose, and what came back.
    """
    body = query_rows(conn, """
        SELECT venue, purpose, status, COUNT(*), SUM(quantity), SUM(filled), ROUND(SUM(dollars), 2), ROUND(SUM(fees), 2), ROUND(AVG(latency_ms))
        FROM orders WHERE sent_at >= ? GROUP BY venue, purpose, status ORDER BY venue, purpose, status""", (since,))
    if body:
        print_table(f"live orders, last {hours} hours", ("venue", "purpose", "status", "orders", "asked", "filled", "dollars $", "fees $", "avg ms"), body)


def print_mode_trades(conn, since, hours, mode):
    total = first_value(conn, "SELECT COUNT(*) FROM trades WHERE mode = ?", (mode,))
    recent = first_value(conn, "SELECT COUNT(*) FROM trades WHERE mode = ? AND signal_ts >= ?", (mode, since))
    covered = conn.execute("SELECT MIN(signal_ts), MAX(signal_ts) FROM trades WHERE mode = ?", (mode,)).fetchone()
    print(f"\ntrades {total:,} {mode} trades in all, from {short_time(covered[0])} to {short_time(covered[1])} UTC, "
          f"{recent:,} in the last {hours} hours")
    if recent:
        body = query_rows(conn, """
            SELECT status, COUNT(*), SUM(quantity), SUM(matched), ROUND(SUM(profit), 2), ROUND(SUM(hedge_pnl), 2), ROUND(SUM(profit + hedge_pnl), 2)
            FROM trades WHERE mode = ? AND signal_ts >= ? GROUP BY status ORDER BY status""", (mode, since))
        print_table(f"{mode} by outcome, last {hours} hours", ("status", "trades", "wanted", "matched", "locked in $", "hedges $", "net $"), body)
        body = query_rows(conn, """
            SELECT p.sport, p.kind, COUNT(*), ROUND(AVG(100 * edge), 1), ROUND(100.0 * SUM(matched) / SUM(quantity), 0),
                   ROUND(SUM(profit + hedge_pnl), 2), ROUND(AVG(yes_latency_ms)), ROUND(AVG(no_latency_ms))
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ? GROUP BY p.sport, p.kind ORDER BY p.sport, p.kind""",
            (mode, since))
        print_table(f"{mode} by kind, last {hours} hours", ("sport", "kind", "trades", "avg edge c", "fill %", "net $", "avg yes ms", "avg no ms"), body)
        best = query_rows(conn, """
            SELECT p.label, trade, quantity, yes_filled, no_filled, ROUND(profit + hedge_pnl, 2), hedge, substr(signal_ts, 12, 8)
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ? ORDER BY profit + hedge_pnl DESC LIMIT 5""", (mode, since))
        print_table(f"{mode} best", ("bet", "trade", "wanted", "yes", "no", "net $", "hedge", "at"),
                    [(l[:40], t, q, y, n, p, h[:40], a) for l, t, q, y, n, p, h, a in best])
        worst = query_rows(conn, """
            SELECT p.label, trade, quantity, yes_filled, no_filled, ROUND(profit + hedge_pnl, 2), hedge, substr(signal_ts, 12, 8)
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ? AND profit + hedge_pnl < 0
            ORDER BY profit + hedge_pnl LIMIT 5""", (mode, since))
        print_table(f"{mode} worst", ("bet", "trade", "wanted", "yes", "no", "net $", "hedge", "at"),
                    [(l[:40], t, q, y, n, p, h[:40], a) for l, t, q, y, n, p, h, a in worst])
    # A settlement's settled_at is its later leg's, so a leg settled in the window is on a settlement that was too,
    # which is checked first, before its trade is looked up.
    settled = query_rows(conn, """
        SELECT venue, COUNT(*), SUM(held), ROUND(SUM(cost), 2), ROUND(SUM(payout), 2), ROUND(SUM(payout - cost), 2) FROM (
            SELECT t.yes_venue AS venue, t.yes_held AS held, t.yes_cost AS cost, s.yes_payout AS payout
            FROM settlements s JOIN trades t ON t.id = s.trade_id
            WHERE s.mode = ? AND s.settled_at >= ? AND s.yes_result IS NOT NULL AND s.yes_settled_at >= ?
            UNION ALL
            SELECT t.no_venue, t.no_held, t.no_cost, s.no_payout
            FROM settlements s JOIN trades t ON t.id = s.trade_id
            WHERE s.mode = ? AND s.settled_at >= ? AND s.no_result IS NOT NULL AND s.no_settled_at >= ?)
        GROUP BY venue ORDER BY venue""", (mode, since, since, mode, since, since))
    if settled:
        print_table(f"{mode} settled legs by venue, last {hours} hours", ("venue", "legs", "contracts", "cost $", "payout $", "realized $"), settled)
    open_count = first_value(conn, """
        SELECT COUNT(*) FROM trades t WHERE mode = ? AND yes_held + no_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)""",
        (mode,))
    print(f"  {open_count:,} {mode} trades still open")
    if mode == "paper":
        print_paper_money(conn)
    else:
        print_live_orders(conn, since, hours)


def read_live_balances(readers=None):
    """
    Each venue's live balance as {venue: dollars, or the reason it could not be read}. readers maps a
    venue to a function returning its dollars; by default the live process's own, which need the keys in data/.
    """
    if readers is None:
        try:
            sys.path.insert(0, str(SRC))
            from engine.components.money.live import READERS as readers
        except Exception as e:
            return {"live balances": f"not read, the venue clients did not load ({e!r})"}
    out = {}
    for venue, read in readers.items():
        try:
            out[venue] = read()
        except Exception as e:
            out[venue] = f"not read ({str(e)[:120]})"
    return out


def print_live_money(conn, balances):
    """
    The live balances on the venues, read now, and the dollars on each venue in live trades still open.
    """
    held = dict(query_rows(conn, """
        SELECT venue, ROUND(SUM(cost), 2) FROM (
            SELECT yes_venue AS venue, yes_cost AS cost FROM trades t
            WHERE mode = 'live' AND yes_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)
            UNION ALL
            SELECT no_venue, no_cost FROM trades t
            WHERE mode = 'live' AND no_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id))
        GROUP BY venue"""))
    print("\nlive money")
    print("  live balances on the venues, read now: " + ", ".join(
        f"{venue} {amount:,.2f}$" if isinstance(amount, (int, float)) else f"{venue} {amount}" for venue, amount in balances.items()))
    if held:
        print("  in open live trades: " + ", ".join(f"{venue} {amount:,.2f}$" for venue, amount in sorted(held.items())))


def print_trades(conn, since, hours):
    """
    The trades of each mode apart, paper first, since paper and live money never mix.
    """
    modes = [m for m, in conn.execute("SELECT DISTINCT mode FROM trades ORDER BY mode DESC")]
    if not modes:
        print("\ntrades: none yet, the live process's executors write them")
        return
    for mode in modes:
        print_mode_trades(conn, since, hours, mode)


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Summarize the SportsArb database.")
    ap.add_argument("--hours", type=int, default=24, help="size of the recent window for feed drops, opportunities, and trades")
    ap.add_argument("--no-live", action="store_true", help="leave out the live balances, which are read from the venues")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).isoformat()
    since = (datetime.fromisoformat(now) - timedelta(hours=args.hours)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    print_storage(conn)
    print_contracts(conn, now)
    print_pairs(conn)
    print_gaps(conn, since, args.hours)
    print_opportunities(conn, since, args.hours)
    print_trades(conn, since, args.hours)
    if not args.no_live:
        print_live_money(conn, read_live_balances())
