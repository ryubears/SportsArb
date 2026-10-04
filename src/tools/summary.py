"""
Print a short summary of the database: the pairs, the opportunities the
executors' rules trade, and the trades.

Opportunities are only those within the rules: an edge of config.MIN_EDGE
or more at the peak, paying config.MIN_PAYOUT_HOURS or more out and
config.MIN_ANNUAL_PCT a year or more. Each shows what it could have taken
at full size through its longest stretch at that edge, and what that
locks in, overall, by sport and kind, and the largest. Trades are shown
for each mode, paper then live, or one with --mode, by outcome and by kind
for the window, the legs settled in it, and the open trades: in a few lines,
how many, how many of them opened in the last hour, day, and week and the
capital those hold, the capital they all hold and the profit they are
expected to return, its rate a year, and when they resolve, then each one
opened in the window. --sport narrows everything to some sports. Reads
only, so it is safe to run while the live process is writing.

The script sets its own import path, so it runs from any folder. The live
money is not in the database but on the venues, so it is read from each
venue with the same read-only calls the live process makes, using the
keys in data/. --no-live leaves that out, and a venue that cannot be read
says why.

Run with:
    python3 src/tools/summary.py
    python3 src/tools/summary.py --hours 6
    python3 src/tools/summary.py --mode paper
    python3 src/tools/summary.py --mode live --sport nfl,ncaaf
    python3 src/tools/summary.py --no-live
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # src, so the script runs from any folder.
from common.timeutil import days_between
from db.database import DB_PATH, read_only
from engine.helper import config

MODES = {"live": ("live",), "paper": ("paper",), "all": ("paper", "live")}     # What --mode shows, in order.
OPENED_HOURS = {"hour": 1, "day": 24, "week": 24 * 7}     # The windows the open trades are counted as opened in.


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


def contracts(value):
    """
    A count of contracts, to the hundredth they are traded in, with thousands separated and no trailing zeros.
    """
    return f"{value or 0:,.2f}".rstrip("0").rstrip(".")


def percent(value):
    """
    A percent for a table cell, or a dash when it cannot be told.
    """
    return "-" if value is None else f"{value:,.1f}"


def print_table(title, header, body, left=1):
    """
    Print a small aligned table with a title line, its first left columns aligned left and the rest right.
    """
    print(f"\n{title}")
    widths = [max(len(str(r[i])) for r in [header] + body) for i in range(len(header))]
    for r in [header] + body:
        print("  " + "  ".join(str(v).ljust(w) if i < left else str(v).rjust(w) for i, (v, w) in enumerate(zip(r, widths))))


def in_sports(sports, column="p.sport"):
    """
    A SQL condition keeping the sports, and its parameters, or nothing when every sport is kept.
    """
    if not sports:
        return "", ()
    return f" AND {column} IN ({', '.join('?' * len(sports))})", tuple(sports)


# SECTIONS

def print_overview(conn, sports):
    """
    The database's size, and the current pairs of each sport in one line.
    """
    size = os.path.getsize(DB_PATH)
    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    if wal.exists():
        size += os.path.getsize(wal)
    where, params = in_sports(sports)
    counts = query_rows(conn, f"""
        SELECT p.sport, COUNT(*) FROM pairs p WHERE p.id IN (SELECT pair_id FROM bets WHERE pair_id IS NOT NULL){where}
        GROUP BY p.sport ORDER BY COUNT(*) DESC""", params)
    total = sum(n for _, n in counts)
    print(f"database {DB_PATH}, {size / 1e6:,.0f} MB, last matched {short_time(first_value(conn, 'SELECT MAX(matched_at) FROM pairs'))} UTC")
    print(f"{total:,} pairs: " + (", ".join(f"{s} {n:,}" for s, n in counts) or "none"))


def print_gaps(conn, since, hours):
    """
    How often each venue's feed dropped in the window, and for how long.
    """
    gaps = query_rows(conn, """
        SELECT venue, COUNT(*), COALESCE(SUM((julianday(end_ts) - julianday(start_ts)) * 86400), 0), SUM(end_ts IS NULL)
        FROM gaps WHERE start_ts >= ? GROUP BY venue ORDER BY venue""", (since,))
    print(f"feed drops, last {hours} hours: " + (", ".join(
        f"{v} {n} ({secs:.0f}s down{f', {open_} without an end' if open_ else ''})" for v, n, secs, open_ in gaps) if gaps else "none"))


def returns(capital, profit, days):
    """
    The return on capital as a percent, and scaled to a year over days held, or None for either that cannot be told.
    """
    ret = 100 * profit / capital if capital > 0 else None
    return ret, (ret * 365 / days if ret is not None and days else None)


def print_opportunities(conn, since, hours, sports):
    """
    The episodes within the rules in the window: at config.MIN_EDGE or more, paying config.MIN_PAYOUT_HOURS or more out
    and config.MIN_ANNUAL_PCT a year or more, both at the peak. Capital is what buying every contract fillable at that
    edge through its longest stretch at it would have cost with fees, and profit what it locks in. The annual rates weight
    each episode by its capital, over the days until it pays.
    """
    cents = f"{100 * config.MIN_EDGE:.0f}c"
    where, params = in_sports(sports)
    rows = query_rows(conn, f"""
        SELECT p.sport, p.kind, p.label, trade, 100 * peak_edge, min_edge_seconds, min_edge_size, min_edge_size - min_edge_profit,
               min_edge_profit, days_held
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id
        WHERE start_ts >= ? AND live = 0 AND peak_edge >= ? AND min_edge_seconds IS NOT NULL AND days_held * 24 >= ? AND annual_pct >= ?{where}
        ORDER BY min_edge_profit DESC""", (since, config.MIN_EDGE, config.MIN_PAYOUT_HOURS, config.MIN_ANNUAL_PCT) + params)
    rules = f"{cents}+, paying {config.MIN_PAYOUT_HOURS}h+ out, {config.MIN_ANNUAL_PCT}%+ a year"
    if not rows:
        print(f"\nopportunities within the rules ({rules}): none in the last {hours} hours")
        return

    def totals(group):
        capital, profit = sum(r[7] for r in group), sum(r[8] for r in group)
        if capital <= 0:
            return capital, profit, None, None, None
        yearly = sum(r[8] * 365 / r[9] for r in group if r[9])
        days = sum(r[7] * r[9] for r in group if r[9]) / capital
        return capital, profit, 100 * profit / capital, 100 * yearly / capital, days

    capital, profit, ret, annual, days = totals(rows)
    print(f"\nopportunities within the rules ({rules}): {len(rows):,} episodes in the last {hours} hours could have taken "
          f"{capital:,.0f}$ and locked in {profit:,.2f}$, {percent(ret)}% on capital, {percent(annual)}% a year, held {days or 0:,.1f} "
          f"days on average")
    kinds = {}
    for r in rows:
        kinds.setdefault((r[0], r[1]), []).append(r)
    body = []
    for (sport, kind), group in sorted(kinds.items()):
        c, pr, r_, a, d = totals(group)
        body.append((sport, kind, len(group), f"{max(r[5] for r in group):,.1f}", f"{c:,.0f}", f"{pr:,.2f}", percent(r_), percent(a), f"{d or 0:,.1f}"))
    print_table("by kind", ("sport", "kind", "episodes", f"longest {cents}+ s", "capital $", "profit $", "return %", "annual %", "avg days"), body)
    largest = []
    for sport, kind, label, trade, peak, seconds, size, cap, pr, d in rows[:5]:
        r_, a = returns(cap, pr, d)
        largest.append((label[:44], f"{peak:.1f}", f"{seconds:,.0f}", f"{size:,.1f}", f"{cap:,.0f}", f"{pr:,.2f}", percent(a), f"{d:,.1f}" if d else "-"))
    print_table("largest", ("bet", "peak c", f"{cents}+ s", "size", "capital $", "profit $", "annual %", "days"), largest)


def print_mode_trades(conn, since, hours, mode, sports, now):
    """
    One mode's trades: how many, by outcome and by kind in the window, the legs settled in it, and the open ones.
    """
    where, params = in_sports(sports)
    total = first_value(conn, f"SELECT COUNT(*) FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ?{where}", (mode,) + params)
    recent = first_value(conn, f"SELECT COUNT(*) FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ?{where}",
                         (mode, since) + params)
    print(f"\n{mode} trades: {total:,} in all, {recent:,} in the last {hours} hours")
    if recent:
        body = query_rows(conn, f"""
            SELECT status, COUNT(*), SUM(quantity), SUM(matched), ROUND(SUM(profit), 2), ROUND(SUM(hedge_pnl), 2), ROUND(SUM(profit + hedge_pnl), 2)
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ?{where} GROUP BY status ORDER BY status""",
            (mode, since) + params)
        print_table(f"{mode} by outcome, last {hours} hours", ("status", "trades", "wanted", "matched", "locked in $", "hedges $", "net $"),
                    [(s, n, contracts(q), contracts(m), *rest) for s, n, q, m, *rest in body])
        body = query_rows(conn, f"""
            SELECT p.sport, p.kind, COUNT(*), ROUND(AVG(100 * edge), 1), ROUND(100.0 * SUM(matched) / SUM(quantity), 0),
                   ROUND(SUM(profit + hedge_pnl), 2), ROUND(AVG(julianday(pays_at) - julianday(signal_ts)), 1)
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ?{where} GROUP BY p.sport, p.kind
            ORDER BY p.sport, p.kind""", (mode, since) + params)
        print_table(f"{mode} by kind, last {hours} hours", ("sport", "kind", "trades", "avg edge c", "fill %", "net $", "avg days held"), body)
    # A settlement's settled_at is its later leg's, so a leg settled in the window is on a settlement that was too,
    # which is checked first, before its trade is looked up.
    settled = query_rows(conn, f"""
        SELECT venue, COUNT(*), SUM(held), ROUND(SUM(cost), 2), ROUND(SUM(payout), 2), ROUND(SUM(payout - cost), 2) FROM (
            SELECT t.yes_venue AS venue, t.yes_held AS held, t.yes_cost AS cost, s.yes_payout AS payout
            FROM settlements s JOIN trades t ON t.id = s.trade_id JOIN pairs p ON p.id = t.pair_id
            WHERE s.mode = ? AND s.settled_at >= ? AND s.yes_result IS NOT NULL AND s.yes_settled_at >= ?{where}
            UNION ALL
            SELECT t.no_venue, t.no_held, t.no_cost, s.no_payout
            FROM settlements s JOIN trades t ON t.id = s.trade_id JOIN pairs p ON p.id = t.pair_id
            WHERE s.mode = ? AND s.settled_at >= ? AND s.no_result IS NOT NULL AND s.no_settled_at >= ?{where})
        GROUP BY venue ORDER BY venue""", (mode, since, since) + params + (mode, since, since) + params)
    if settled:
        print_table(f"{mode} settled legs by venue, last {hours} hours", ("venue", "legs", "contracts", "cost $", "payout $", "realized $"), settled)
    print_open_trades(conn, since, hours, mode, sports, now)


def date_at(seconds):
    """
    The UTC date at seconds since 1970.
    """
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")


def print_open_trades(conn, since, hours, mode, sports, now):
    """
    The open trades of one mode in a few lines, then each one opened in the window, newest first. Capital is what the
    contracts still held cost with fees, and the expected profit what the trades locked in, a dollar for each pair held
    less what it cost, with what flattening made or lost. A contract held without its other side, which the held column
    shows, is counted at its cost, as if it broke even. The rate a year scales each trade's return over the days from the
    trade to its payout, weighted by capital. They resolve from the first payout, which may be past and waiting on a
    venue, to the last, and the average weighted by capital is when the money comes back on average.
    """
    where, params = in_sports(sports)
    rows = query_rows(conn, f"""
        SELECT t.id, p.sport, p.label, t.yes_held, t.no_held, t.yes_venue, t.yes_cost, t.no_venue, t.no_cost, t.profit + t.hedge_pnl,
               t.signal_ts, t.pays_at
        FROM trades t JOIN pairs p ON p.id = t.pair_id
        WHERE t.mode = ? AND t.yes_held + t.no_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id){where}
        ORDER BY t.pays_at, t.id""", (mode,) + params)
    if not rows:
        print(f"\n{mode} open trades: none")
        return
    capital, profit, yearly, when, venues, body = 0.0, 0.0, 0.0, 0.0, {}, []
    for trade_id, sport, label, yes, no, yes_venue, yes_cost, no_venue, no_cost, pr, signal, pays in rows:
        cap, days = yes_cost + no_cost, max(days_between(signal, pays), 1 / 24)
        capital, profit, yearly = capital + cap, profit + pr, yearly + pr * 365 / days
        when += cap * datetime.fromisoformat(pays).timestamp()
        for venue, cost in ((yes_venue, yes_cost), (no_venue, no_cost)):
            venues[venue] = venues.get(venue, 0.0) + cost
        if signal >= since:
            ret, annual = returns(cap, pr, days)
            body.append((signal, trade_id, sport, label[:44], short_time(signal)[:16], f"{contracts(yes)}/{contracts(no)}", f"{cap:,.2f}",
                         f"{pr:,.2f}", percent(ret), percent(annual), short_time(pays)[:16]))
    opened = []
    for name, window in OPENED_HOURS.items():
        group = [r for r in rows if days_between(r[10], now) * 24 <= window]
        opened.append(f"{len(group):,}{'' if opened else ' opened'} in the last {name} using {sum(r[6] + r[8] for r in group):,.2f}$")
    ret, annual = (100 * profit / capital, 100 * yearly / capital) if capital > 0 else (None, None)
    held = ", ".join(f"{venue} {amount:,.2f}$" for venue, amount in sorted(venues.items()))
    print(f"\n{mode} open trades: {len(rows):,} open, {', '.join(opened)}")
    print(f"  capital {capital:,.2f}$, holding {held}, expected to return {profit:,.2f}$ in profit, {percent(ret)}% on it, "
          f"{percent(annual)}% a year")
    print(f"  resolving first {rows[0][11][:10]}, on average {date_at(when / capital) if capital > 0 else '-'}, last {rows[-1][11][:10]}")
    if not body:
        print(f"  none opened in the last {hours} hours")
        return
    body.sort(reverse=True)
    print_table(f"{mode} open trades opened in the last {hours} hours, newest first",
                ("trade", "sport", "bet", "opened UTC", "held yes/no", "capital $", "profit $", "return %", "annual %", "pays UTC"),
                [r[1:] for r in body], left=3)


def print_paper_money(conn):
    """
    The paper balances from the ledger. Live money is on the venues, see print_live_money().
    """
    balances = query_rows(conn, "SELECT venue, ROUND(balance, 2) FROM ledger WHERE id IN (SELECT MAX(id) FROM ledger GROUP BY venue) ORDER BY venue")
    if balances:
        print("\npaper money from the ledger: " + ", ".join(f"{v} {a:,.2f}$" for v, a in balances))


def print_live_orders(conn, since, hours, sports):
    """
    The real orders sent in the window, by venue, purpose, and what came back.
    """
    where, params = in_sports(sports)
    body = query_rows(conn, f"""
        SELECT o.venue, purpose, o.status, COUNT(*), SUM(o.quantity), SUM(filled), ROUND(SUM(dollars), 2), ROUND(SUM(fees), 2), ROUND(AVG(latency_ms))
        FROM orders o JOIN trades t ON t.id = o.trade_id JOIN pairs p ON p.id = t.pair_id WHERE sent_at >= ?{where}
        GROUP BY o.venue, purpose, o.status ORDER BY o.venue, purpose, o.status""", (since,) + params)
    if body:
        print_table(f"live orders, last {hours} hours", ("venue", "purpose", "status", "orders", "asked", "filled", "dollars $", "fees $", "avg ms"),
                    [(v, p, s, n, contracts(q), contracts(f), *rest) for v, p, s, n, q, f, *rest in body])


def print_trades(conn, since, hours, modes=("live",), sports=(), now=None):
    """
    The trades of each mode asked for apart, since paper and live money never mix.
    """
    now = now or datetime.now(timezone.utc).isoformat()
    for mode in modes:
        print_mode_trades(conn, since, hours, mode, sports, now)
        if mode == "paper":
            print_paper_money(conn)
        else:
            print_live_orders(conn, since, hours, sports)


def read_live_balances(readers=None):
    """
    Each venue's live balance as {venue: (dollars, {exchange shard: dollars}), or the reason it could not be read}.
    readers maps a venue to a function returning those; by default the live process's own, which need the keys in
    data/. They are loaded only here, so the rest of the report runs without the venue clients.
    """
    if readers is None:
        try:
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


def print_live_money(balances):
    """
    The live balances on the venues, read now, with the exchange shards live trading uses, config.LIVE_SHARDS, and
    any other holding money. What open live trades hold is with the live trades.
    """
    def money(venue, reading):
        if isinstance(reading, str):
            return f"{venue} {reading}"
        dollars, shards = reading
        parts = [f"shard {s} {a:,.2f}$" for s, a in sorted(shards.items()) if a or s in config.LIVE_SHARDS.get(venue, ())]
        return f"{venue} {dollars:,.2f}$" + (f" ({', '.join(parts)})" if parts else "")

    print("\nlive money on the venues, read now: " + ", ".join(money(venue, reading) for venue, reading in balances.items()))


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Summarize the SportsArb database.")
    ap.add_argument("--hours", type=int, default=24, help="size of the recent window for feed drops, opportunities, and trades")
    ap.add_argument("--mode", choices=sorted(MODES), default="all", help="the trades to show: live, paper, or all, the default")
    ap.add_argument("--sport", default="", help="the sports to show, comma separated, every sport when left out")
    ap.add_argument("--no-live", action="store_true", help="leave out the live balances, which are read from the venues")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).isoformat()
    since = (datetime.fromisoformat(now) - timedelta(hours=args.hours)).isoformat()
    sports = tuple(s.strip() for s in args.sport.split(",") if s.strip())
    conn = read_only()
    print_overview(conn, sports)
    print_gaps(conn, since, args.hours)
    print_opportunities(conn, since, args.hours, sports)
    print_trades(conn, since, args.hours, MODES[args.mode], sports, now)
    if "live" in MODES[args.mode] and not args.no_live:
        print_live_money(read_live_balances())
