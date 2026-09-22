"""
Print a summary of everything in the database.

Row counts and time ranges for each table, recording health for the
recent window, pairs by kind, and the opportunities found so far. Reads
only, so it is safe to run while the recorder is writing.

This script opens the database file directly rather than importing the
db package, so it runs from any folder without setting an import path.

Run with:
    python3 src/db/summary.py
    python3 src/db/summary.py --hours 6
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
    tables = ["contracts", "bets", "pairs", "quotes", "stream_gaps", "opportunities"]
    print_table("tables", ("table", "rows"), [(t, f"{first_value(conn, f'SELECT COUNT(*) FROM {t}'):,}") for t in tables])


def print_contracts(conn, now):
    body = query_rows(conn, """
        SELECT venue, sport, COUNT(*), SUM(close_time IS NULL OR close_time > ?), MAX(last_seen)
        FROM contracts GROUP BY venue, sport ORDER BY venue, sport""", (now,))
    print_table("contracts", ("venue", "sport", "total", "open", "last fetched"),
                [(v, s, f"{n:,}", f"{o:,}", short_time(t)) for v, s, n, o, t in body])


def print_pairs(conn):
    body = query_rows(conn, """
        SELECT kind, venues, COUNT(*), SUM(contracts), SUM(game_date IS NOT NULL)
        FROM pairs GROUP BY kind, venues ORDER BY kind, COUNT(*) DESC""")
    print_table("pairs", ("kind", "venues", "pairs", "contracts", "games"), body)
    print(f"  total {first_value(conn, 'SELECT COUNT(*) FROM pairs'):,}, "
          f"last matched {short_time(first_value(conn, 'SELECT MAX(matched_at) FROM pairs'))}")


def print_quotes(conn, now, hours):
    total, first, last = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM quotes").fetchone()
    print(f"\nquotes {total:,} rows from {short_time(first)} to {short_time(last)} UTC")
    if not total:
        return
    since = (datetime.fromisoformat(now) - timedelta(hours=hours)).isoformat()
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
        FROM stream_gaps WHERE start_ts >= ? GROUP BY venue ORDER BY venue""", (since,))
    if gaps:
        print("  feed drops in the window: " + ", ".join(
            f"{v} {n} ({secs:.0f}s down{f', {open_} without an end' if open_ else ''})" for v, n, secs, open_ in gaps))
    busiest = query_rows(conn, """
        SELECT q.venue, COALESCE(c.title || ' / ' || c.outcome, q.contract_id), COUNT(*) FROM quotes q
        LEFT JOIN contracts c ON c.venue = q.venue AND c.contract_id = q.contract_id
        WHERE q.ts >= ? GROUP BY q.venue, q.contract_id ORDER BY COUNT(*) DESC LIMIT 5""", (since,))
    print_table("busiest contracts in the window", ("venue", "contract", "rows"),
                [(v, c[:60], f"{n:,}") for v, c, n in busiest])


def print_opportunities(conn):
    total = first_value(conn, "SELECT COUNT(*) FROM opportunities")
    if not total:
        print("\nopportunities: none yet, the recorder's scanner writes them")
        return
    covered = conn.execute("SELECT MIN(start_ts), MAX(end_ts) FROM opportunities").fetchone()
    print(f"\nopportunities {total:,} episodes, covering {short_time(covered[0])} to {short_time(covered[1])} UTC")
    # Capital required is the fillable size times the cost of both legs and fees, which is one dollar minus the edge.
    body = query_rows(conn, """
        SELECT kind, COUNT(*), SUM(live), ROUND(100 * MAX(peak_edge), 1), ROUND(MAX(peak_profit), 2),
               ROUND(MAX(peak_size * (1 - peak_edge))), ROUND(MAX(return_pct), 2), ROUND(MAX(annual_pct)),
               ROUND(AVG(days_held), 1), SUM(annual_pct >= 10)
        FROM opportunities GROUP BY kind ORDER BY kind""")
    print_table("by kind", ("kind", "episodes", "live", "best edge c", "best profit $", "max capital $",
                            "best return %", "best annual %", "avg days held", "beat 10%/yr"), body)
    best = query_rows(conn, """
        SELECT label, trade, ROUND(100 * peak_edge, 1), ROUND(peak_size), ROUND(peak_size * (1 - peak_edge)),
               ROUND(peak_profit, 2), ROUND(return_pct, 2), ROUND(annual_pct), ROUND(days_held, 1), ROUND(seconds), live
        FROM opportunities WHERE annual_pct >= 10 ORDER BY peak_profit DESC LIMIT 8""")
    print_table("largest that beat the target",
                ("bet", "trade", "edge c", "size", "capital $", "profit $", "return %", "annual %", "days held", "seconds", "live"),
                [(l[:40], t, e, f"{s:,.0f}", f"{cap:,.0f}", p, r, f"{a:,.0f}", d, f"{sec:,.0f}", "yes" if lv else "")
                 for l, t, e, s, cap, p, r, a, d, sec, lv in best])


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Summarize the SportsArb database.")
    ap.add_argument("--hours", type=int, default=24, help="size of the recent window for quote stats")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    print_storage(conn)
    print_contracts(conn, now)
    print_pairs(conn)
    print_quotes(conn, now, args.hours)
    print_opportunities(conn)
