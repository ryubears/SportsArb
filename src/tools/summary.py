"""
Print a summary of everything in the database.

Row counts and time ranges for each table, pairs by sport and kind, and
for the recent window the feed drops, the opportunities found, and the
trades made, paper and live apart. Live game opportunities, during a
game, are shown apart from before game and futures ones, whose money is
tied up longer. Each group shows all its episodes, then only those whose
edge reached config.MIN_EDGE: how long each held there, and what it could
have taken at full size, locked in, and returned. Before game and futures
ones are shown a third time, only those the executors' rules trade:
paying config.MIN_PAYOUT_HOURS or more out and config.MIN_ANNUAL_PCT a
year or more. Games are not traded once they kick off. Open trades are
listed soonest to settle first, each with the capital it holds, the
profit it is expected to make, and that as a return and a year's rate,
with the average over all of them. Reads only, so it is safe to run while
the live process is writing.

The script sets its own import path, so it runs from any folder. The live
money is not in the database but on the venues, so it is read from each
venue with the same read-only calls the live process makes, using the
keys in data/. --no-live leaves that out, and a venue that cannot be read
says why.

Run with:
    python3 src/tools/summary.py
    python3 src/tools/summary.py --hours 6
    python3 src/tools/summary.py --no-live
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # src, so the script runs from any folder.
from common.timeutil import days_between
from db.database import DB_PATH, load_held, read_only
from engine.helper import config


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
    tables = ["contracts", "bets", "pairs", "gaps", "opportunities", "trades", "settlements", "orders", "ledger", "alerts"]
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


def print_all_episodes(conn, since, hours, live, name):
    """
    Every episode of one group in the window, live game ones when live is 1 and before game or futures ones when 0:
    by kind, then the largest at their peak. Capital is the contracts times the cost of both legs and fees, which is
    one dollar a contract less the edge.
    """
    count = first_value(conn, "SELECT COUNT(*) FROM opportunities WHERE start_ts >= ? AND live = ?", (since, live))
    print(f"\n{name}: {count:,} episodes in the last {hours} hours")
    if not count:
        return
    target = config.MIN_ANNUAL_PCT
    body = query_rows(conn, """
        SELECT p.sport, p.kind, COUNT(*), ROUND(100 * MAX(peak_edge), 1), ROUND(MAX(peak_profit), 2), MAX(peak_size * (1 - peak_edge)),
               MAX(annual_pct), ROUND(AVG(days_held), 1), SUM(annual_pct >= ?)
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id WHERE start_ts >= ? AND live = ? GROUP BY p.sport, p.kind ORDER BY p.sport, p.kind""",
        (target, since, live))
    print_table(f"{name}, by kind, last {hours} hours",
                ("sport", "kind", "episodes", "best edge c", "best profit $", "max capital $", "best annual %", "avg days held", f"beat {target}%/yr"),
                [(sp, k, n, e, pr, f"{cap:,.0f}", f"{a:,.0f}" if a is not None else "-", d, beat) for sp, k, n, e, pr, cap, a, d, beat in body])
    best = query_rows(conn, """
        SELECT p.label, trade, ROUND(100 * peak_edge, 1), peak_size, peak_size * (1 - peak_edge), ROUND(peak_profit, 2), annual_pct,
               ROUND(days_held, 1), ROUND(seconds, 1)
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id WHERE start_ts >= ? AND live = ? ORDER BY peak_profit DESC LIMIT 5""", (since, live))
    print_table(f"{name}, largest at their peak, last {hours} hours",
                ("bet", "trade", "edge c", "size", "capital $", "profit $", "annual %", "days held", "seconds"),
                [(l[:44], t, e, f"{n:,.0f}", f"{cap:,.0f}", pr, f"{a:,.0f}" if a is not None else "-", d, f"{sec:,.1f}")
                 for l, t, e, n, cap, pr, a, d, sec in best])


def lasting_returns(capital, profit, days):
    """
    The return on capital as a percent, and scaled to a year over days held, or None for either that cannot be told.
    """
    ret = 100 * profit / capital if capital > 0 else None
    return ret, (ret * 365 / days if ret is not None and days else None)


def percent(value):
    """
    A percent for a table cell, or a dash when it cannot be told.
    """
    return "-" if value is None else f"{value:,.1f}"


def print_min_edge_episodes(conn, since, hours, live, name, rules=False):
    """
    The episodes of one group whose edge reached config.MIN_EDGE: how long each held there, and what it could have
    taken at full size and locked in, overall, by kind, and the largest. Capital is what buying every contract
    fillable at that edge through the longest stretch at it would have cost with fees, and profit what it locks in.
    The annual rates weight each episode by its capital, over the days until it pays. With rules, only those the
    executors trade: at their peak, paying config.MIN_PAYOUT_HOURS or more out and config.MIN_ANNUAL_PCT a year or more.
    """
    cents = f"{100 * config.MIN_EDGE:.0f}c"
    title = f"{name} {'within the rules' if rules else f'at {cents}+'}"
    within = " AND days_held * 24 >= ? AND annual_pct >= ?" if rules else ""
    rows = query_rows(conn, f"""
        SELECT p.sport, p.kind, p.label, trade, 100 * peak_edge, min_edge_seconds, min_edge_size, min_edge_size - min_edge_profit,
               min_edge_profit, days_held
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id
        WHERE start_ts >= ? AND live = ? AND peak_edge >= ? AND min_edge_seconds IS NOT NULL{within} ORDER BY min_edge_profit DESC""",
        (since, live, config.MIN_EDGE) + ((config.MIN_PAYOUT_HOURS, config.MIN_ANNUAL_PCT) if rules else ()))
    if not rows:
        print(f"\n{title}: none in the last {hours} hours")
        return

    def totals(group):
        capital, profit = sum(r[7] for r in group), sum(r[8] for r in group)
        if capital <= 0:
            return capital, profit, None, None, None
        yearly = sum(r[8] * 365 / r[9] for r in group if r[9])
        days = sum(r[7] * r[9] for r in group if r[9]) / capital
        return capital, profit, 100 * profit / capital, 100 * yearly / capital, days

    capital, profit, ret, annual, days = totals(rows)
    print(f"\n{title}: {len(rows):,} episodes in the last {hours} hours could have taken {capital:,.0f}$ and locked in {profit:,.2f}$, "
          f"{percent(ret)}% on capital, {percent(annual)}% a year, held {days or 0:,.1f} days on average")
    kinds = {}
    for r in rows:
        kinds.setdefault((r[0], r[1]), []).append(r)
    body = []
    for (sport, kind), group in sorted(kinds.items()):
        c, pr, r_, a, d = totals(group)
        body.append((sport, kind, len(group), f"{max(r[5] for r in group):,.1f}", f"{c:,.0f}", f"{pr:,.2f}", percent(r_), percent(a), f"{d or 0:,.1f}"))
    print_table(f"{title}, by kind, last {hours} hours",
                ("sport", "kind", "episodes", f"longest {cents}+ s", "capital $", "profit $", "return %", "annual %", "avg days held"), body)
    largest = []
    for sport, kind, label, trade, peak, seconds, size, cap, pr, d in rows[:8]:
        r_, a = lasting_returns(cap, pr, d)
        largest.append((label[:44], trade, f"{peak:.1f}", f"{seconds:,.1f}", f"{size:,.1f}", f"{cap:,.0f}", f"{pr:,.2f}", percent(r_), percent(a),
                        f"{d:,.1f}" if d else "-"))
    print_table(f"{title}, largest, last {hours} hours",
                ("bet", "trade", "peak c", f"{cents}+ s", "size", "capital $", "profit $", "return %", "annual %", "days held"), largest)
    if rules:
        print(f"  Within the rules is what the executors trade: {cents} or more, paying {config.MIN_PAYOUT_HOURS}h or more out and "
              f"{config.MIN_ANNUAL_PCT}% a year or more, both at the peak.")
    else:
        print(f"  {cents}+ s is how long the edge held at {cents} or more without a break. Capital is every contract fillable at {cents} or "
              f"more through all of that, at its cost with fees, and profit what that locks in, at full size. An edge that comes and goes "
              f"counts again each time it does.")


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
    if recent:
        for live, name in ((1, "live game opportunities"), (0, "before game/futures opportunities")):
            print_all_episodes(conn, since, hours, live, name)
            print_min_edge_episodes(conn, since, hours, live, name)
            if live:
                print("  Live game opportunities are not traded: a game is traded only before it kicks off.")
            else:
                print_min_edge_episodes(conn, since, hours, live, name, rules=True)


def print_paper_money(conn):
    """
    The paper balances from the ledger. Live money is on the venues, see live_check.py.
    """
    balances = query_rows(conn, "SELECT venue, ROUND(balance, 2) FROM ledger WHERE id IN (SELECT MAX(id) FROM ledger GROUP BY venue) ORDER BY venue")
    if balances:
        print("  paper balances from the ledger: " + ", ".join(f"{v} {a:,.2f}$" for v, a in balances))


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
                   ROUND(SUM(profit + hedge_pnl), 2), ROUND(AVG(julianday(pays_at) - julianday(signal_ts)), 1),
                   ROUND(AVG(yes_latency_ms)), ROUND(AVG(no_latency_ms))
            FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ? GROUP BY p.sport, p.kind ORDER BY p.sport, p.kind""",
            (mode, since))
        print_table(f"{mode} by kind, last {hours} hours",
                    ("sport", "kind", "trades", "avg edge c", "fill %", "net $", "avg days held", "avg yes ms", "avg no ms"), body)
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
    print_open_trades(conn, mode)
    if mode == "paper":
        print_paper_money(conn)
    else:
        print_live_orders(conn, since, hours)


def print_open_trades(conn, mode):
    """
    The trades of one mode still open, soonest to settle first. Capital is
    what the contracts still held cost with fees, and the expected profit
    what the trade locked in, a dollar for each pair held less what it cost,
    with what flattening made or lost. A contract held without its other
    side, which the held column shows, is counted at its cost, as if it broke
    even. The return is on the capital, and the annual rate scales it over
    the days from the trade to its payout. Over all of them the annual rate
    is weighted by capital.
    """
    rows = query_rows(conn, """
        SELECT p.label, t.yes_held, t.no_held, t.yes_cost + t.no_cost, t.profit + t.hedge_pnl, t.signal_ts, t.pays_at
        FROM trades t JOIN pairs p ON p.id = t.pair_id
        WHERE t.mode = ? AND t.yes_held + t.no_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)
        ORDER BY t.pays_at, t.id""", (mode,))
    if not rows:
        print(f"  0 {mode} trades still open")
        return
    body, capital, profit, yearly = [], 0.0, 0.0, 0.0
    for label, yes, no, cap, pr, signal, pays in rows:
        days = max(days_between(signal, pays), 1 / 24)
        ret, annual = lasting_returns(cap, pr, days)
        body.append((label[:44], f"{yes}/{no}", f"{cap:,.2f}", f"{pr:,.2f}", percent(ret), percent(annual), f"{days:,.1f}",
                     pays[:16].replace("T", " ")))
        capital, profit, yearly = capital + cap, profit + pr, yearly + pr * 365 / days
    held = ", ".join(f"{venue} {amount:,.2f}$" for venue, amount in sorted(load_held(conn, mode).items()))
    ret, annual = (100 * profit / capital, 100 * yearly / capital) if capital > 0 else (None, None)
    print(f"  {len(rows):,} {mode} trades still open, holding {held}: {capital:,.2f}$ of capital expected to make {profit:,.2f}$, "
          f"{percent(ret)}% on it, {percent(annual)}% a year on average, settling from {rows[0][6][:10]} to {rows[-1][6][:10]}")
    print_table(f"{mode} open trades, soonest to settle first",
                ("bet", "held yes/no", "capital $", "expected profit $", "return %", "annual %", "days held", "settles UTC"), body)


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

    print("\nlive money")
    print("  live balances on the venues, read now: " + ", ".join(money(venue, reading) for venue, reading in balances.items()))


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
    conn = read_only()
    print_storage(conn)
    print_contracts(conn, now)
    print_pairs(conn)
    print_gaps(conn, since, args.hours)
    print_opportunities(conn, since, args.hours)
    print_trades(conn, since, args.hours)
    if not args.no_live:
        print_live_money(read_live_balances())
