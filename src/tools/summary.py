"""
Print a summary of everything in the database.

Row counts and time ranges for each table, pairs by kind, and for the
recent window the recording health, the opportunities found, and the
paper trades made. Reads
only, so it is safe to run while the recorder is writing.

This script opens the database file directly rather than importing the
db package, so it runs from any folder without setting an import path.

Run with:
    python3 src/tools/summary.py
    python3 src/tools/summary.py --hours 6
"""

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sportsarb.sqlite"


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
    tables = ["contracts", "bets", "pairs", "quotes", "gaps", "opportunities", "trades", "ledger", "transfers"]
    print_table("tables", ("table", "rows"), [(t, f"{first_value(conn, f'SELECT COUNT(*) FROM {t}'):,}") for t in tables])


def print_contracts(conn, now):
    body = query_rows(conn, """
        SELECT venue, sport, COUNT(*), SUM(close_time IS NULL OR close_time > ?), MAX(last_seen)
        FROM contracts GROUP BY venue, sport ORDER BY venue, sport""", (now,))
    print_table("contracts", ("venue", "sport", "total", "open", "last fetched"),
                [(v, s, f"{n:,}", f"{o:,}", short_time(t)) for v, s, n, o, t in body])


def print_pairs(conn):
    body = query_rows(conn, """
        SELECT kind, COUNT(*), SUM(contracts), SUM(game_date IS NOT NULL)
        FROM pairs WHERE id IN (SELECT pair_id FROM bets WHERE pair_id IS NOT NULL) GROUP BY kind ORDER BY kind""")
    print_table("pairs", ("kind", "pairs", "contracts", "games"), body)
    print(f"  total {first_value(conn, 'SELECT COUNT(*) FROM pairs WHERE id IN (SELECT pair_id FROM bets WHERE pair_id IS NOT NULL)'):,}, "
          f"last matched {short_time(first_value(conn, 'SELECT MAX(matched_at) FROM pairs'))}")


def print_quotes(conn, since, hours):
    total, first, last = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM quotes").fetchone()
    print(f"\nquotes {total:,} rows from {short_time(first)} to {short_time(last)} UTC")
    if not total:
        return
    body = query_rows(conn, """
        SELECT venue, COUNT(*), COUNT(DISTINCT contract_id), MAX(ts)
        FROM quotes WHERE ts >= ? GROUP BY venue ORDER BY venue""", (since,))
    print_table(f"last {hours} hours", ("venue", "rows", "contracts", "latest"),
                [(v, f"{n:,}", f"{c:,}", short_time(t)) for v, n, c, t in body])
    per_hour = query_rows(conn, """
        SELECT substr(ts, 1, 13), COUNT(*) FROM quotes WHERE ts >= ?
        GROUP BY substr(ts, 1, 13) ORDER BY 1""", (since,))
    if per_hour:
        counts = [n for _, n in per_hour]
        print(f"  rows per hour: min {min(counts):,}, median {sorted(counts)[len(counts) // 2]:,}, max {max(counts):,} "
              f"over {len(counts)} hours")
        quiet = [h for h, n in per_hour if n < max(counts) / 20]
        if quiet:
            print(f"  quiet hours (under a twentieth of the busiest): {', '.join(h.replace('T', ' ') + ':00' for h in quiet)}")
    gaps = query_rows(conn, """
        SELECT venue, COUNT(*), COALESCE(SUM((julianday(end_ts) - julianday(start_ts)) * 86400), 0), SUM(end_ts IS NULL)
        FROM gaps WHERE start_ts >= ? GROUP BY venue ORDER BY venue""", (since,))
    if gaps:
        print("  feed drops in the window: " + ", ".join(
            f"{v} {n} ({secs:.0f}s down{f', {open_} without an end' if open_ else ''})" for v, n, secs, open_ in gaps))
    busiest = query_rows(conn, """
        SELECT q.venue, COALESCE(c.title || ' / ' || c.outcome, q.contract_id), COUNT(*) FROM quotes q
        LEFT JOIN contracts c ON c.venue = q.venue AND c.contract_id = q.contract_id
        WHERE q.ts >= ? GROUP BY q.venue, q.contract_id ORDER BY COUNT(*) DESC LIMIT 5""", (since,))
    print_table("busiest contracts in the window", ("venue", "contract", "rows"),
                [(v, c[:60], f"{n:,}") for v, c, n in busiest])


def print_opportunities(conn, since, hours):
    total = first_value(conn, "SELECT COUNT(*) FROM opportunities")
    recent = first_value(conn, "SELECT COUNT(*) FROM opportunities WHERE start_ts >= ?", (since,))
    if not total:
        print("\nopportunities: none yet, the recorder's scanner writes them")
        return
    covered = conn.execute("SELECT MIN(start_ts), MAX(end_ts) FROM opportunities").fetchone()
    print(f"\nopportunities {total:,} episodes in all, covering {short_time(covered[0])} to {short_time(covered[1])} UTC, "
          f"{recent:,} in the last {hours} hours")
    if not recent:
        return
    # Capital required is the fillable size times the cost of both legs and fees, which is one dollar minus the edge.
    body = query_rows(conn, """
        SELECT p.kind, COUNT(*), SUM(live), ROUND(100 * MAX(peak_edge), 1), ROUND(MAX(peak_profit), 2),
               ROUND(MAX(peak_size * (1 - peak_edge))), ROUND(MAX(return_pct), 2), ROUND(MAX(annual_pct)),
               ROUND(AVG(days_held), 1), SUM(annual_pct >= 10)
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id WHERE start_ts >= ? GROUP BY p.kind ORDER BY p.kind""", (since,))
    print_table(f"by kind, last {hours} hours", ("kind", "episodes", "live", "best edge c", "best profit $", "max capital $",
                            "best return %", "best annual %", "avg days held", "beat 10%/yr"), body)
    best = query_rows(conn, """
        SELECT p.label, trade, ROUND(100 * peak_edge, 1), ROUND(peak_size), ROUND(peak_size * (1 - peak_edge)),
               ROUND(peak_profit, 2), ROUND(return_pct, 2), ROUND(annual_pct), ROUND(days_held, 1), ROUND(seconds), live
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id WHERE start_ts >= ? AND annual_pct >= 10 ORDER BY peak_profit DESC LIMIT 8""", (since,))
    print_table(f"largest that beat the target, last {hours} hours",
                ("bet", "trade", "edge c", "size", "capital $", "profit $", "return %", "annual %", "days held", "seconds", "live"),
                [(l[:40], t, e, f"{s:,.0f}", f"{cap:,.0f}", p, r, f"{a:,.0f}", d, f"{sec:,.0f}", "yes" if lv else "")
                 for l, t, e, s, cap, p, r, a, d, sec, lv in best])


def print_trades(conn, since, hours):
    total = first_value(conn, "SELECT COUNT(*) FROM trades")
    recent = first_value(conn, "SELECT COUNT(*) FROM trades WHERE signal_ts >= ?", (since,))
    if not total:
        print("\ntrades: none yet, the recorder's paper executor writes them")
        return
    covered = conn.execute("SELECT MIN(signal_ts), MAX(signal_ts) FROM trades").fetchone()
    print(f"\ntrades {total:,} paper trades in all, from {short_time(covered[0])} to {short_time(covered[1])} UTC, "
          f"{recent:,} in the last {hours} hours")
    if recent:
        body = query_rows(conn, """
            SELECT status, COUNT(*), SUM(quantity), SUM(matched), ROUND(SUM(profit), 2), ROUND(SUM(hedge_pnl), 2), ROUND(SUM(profit + hedge_pnl), 2)
            FROM trades WHERE signal_ts >= ? GROUP BY status ORDER BY status""", (since,))
        print_table(f"by outcome, last {hours} hours", ("status", "trades", "wanted", "matched", "locked in $", "hedges $", "net $"), body)
        body = query_rows(conn, """
            SELECT p.kind, COUNT(*), ROUND(AVG(100 * edge), 1), ROUND(100.0 * SUM(matched) / SUM(quantity), 0), ROUND(SUM(profit + hedge_pnl), 2),
                   ROUND(AVG(yes_latency_ms)), ROUND(AVG(no_latency_ms))
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE signal_ts >= ? GROUP BY p.kind ORDER BY p.kind""", (since,))
        print_table(f"by kind, last {hours} hours", ("kind", "trades", "avg edge c", "fill %", "net $", "avg yes ms", "avg no ms"), body)
        best = query_rows(conn, """
            SELECT p.label, trade, quantity, yes_filled, no_filled, ROUND(profit + hedge_pnl, 2), hedge, substr(signal_ts, 12, 8)
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE signal_ts >= ? ORDER BY profit + hedge_pnl DESC LIMIT 5""", (since,))
        print_table("best", ("bet", "trade", "wanted", "yes", "no", "net $", "hedge", "at"),
                    [(l[:40], t, q, y, n, p, h[:40], a) for l, t, q, y, n, p, h, a in best])
        worst = query_rows(conn, """
            SELECT p.label, trade, quantity, yes_filled, no_filled, ROUND(profit + hedge_pnl, 2), hedge, substr(signal_ts, 12, 8)
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE signal_ts >= ? AND profit + hedge_pnl < 0 ORDER BY profit + hedge_pnl LIMIT 5""", (since,))
        print_table("worst", ("bet", "trade", "wanted", "yes", "no", "net $", "hedge", "at"),
                    [(l[:40], t, q, y, n, p, h[:40], a) for l, t, q, y, n, p, h, a in worst])
    settled = query_rows(conn, """
        SELECT venue, COUNT(*), SUM(held), ROUND(SUM(cost), 2), ROUND(SUM(payout), 2), ROUND(SUM(payout - cost), 2) FROM (
            SELECT yes_venue AS venue, yes_held AS held, yes_cost AS cost, yes_payout AS payout, yes_settled_at AS settled_at
            FROM trades WHERE yes_result IS NOT NULL
            UNION ALL
            SELECT no_venue, no_held, no_cost, no_payout, no_settled_at FROM trades WHERE no_result IS NOT NULL)
        WHERE settled_at >= ? GROUP BY venue ORDER BY venue""", (since,))
    if settled:
        print_table(f"settled legs by venue, last {hours} hours", ("venue", "legs", "contracts", "cost $", "payout $", "realized $"), settled)
    open_count = first_value(conn, "SELECT COUNT(*) FROM trades WHERE settled_at IS NULL AND yes_held + no_held > 0")
    print(f"  {open_count:,} trades still open")
    balances = query_rows(conn, "SELECT venue, ROUND(balance, 2) FROM ledger WHERE id IN (SELECT MAX(id) FROM ledger GROUP BY venue) ORDER BY venue")
    if balances:
        print("  balances from the ledger: " + ", ".join(f"{v} {a:,.2f}$" for v, a in balances))
    transfers = query_rows(conn, "SELECT from_venue, to_venue, ROUND(amount), reason, substr(requested_at, 1, 10), substr(arrived_at, 1, 10) FROM transfers ORDER BY id DESC LIMIT 5")
    if transfers:
        print_table("transfers", ("from", "to", "amount $", "reason", "requested", "arrived"), [(f, t, a, r, q, v or "in transit") for f, t, a, r, q, v in transfers])


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Summarize the SportsArb database.")
    ap.add_argument("--hours", type=int, default=24, help="size of the recent window for quotes, opportunities, and trades")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).isoformat()
    since = (datetime.fromisoformat(now) - timedelta(hours=args.hours)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    print_storage(conn)
    print_contracts(conn, now)
    print_pairs(conn)
    print_quotes(conn, since, args.hours)
    print_opportunities(conn, since, args.hours)
    print_trades(conn, since, args.hours)
