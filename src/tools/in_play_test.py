"""
Print how the live trades of the in-play test did beside their paper twins.

In the in-play test, which engine.run --live-in-play starts, live trades a
few contracts of the games, matches, races, and windows under way, and paper
sends a twin of each: the same signal, the same legs, the same limits, the
same size, giving back what our live orders took from the books, see
trading/live.py and trading/paper.py. The twins table pairs them. Where
the two did alike, paper's fills in play are ones live gets; where live did
worse, paper is optimistic there.

For the live trades and their twins it prints, side by side, how many
filled in full, in part, on one leg only, or not at all, the contracts
asked for and matched, how often each venue's leg filled, what was locked
in and what the sales back made, the result of those settled, counting
what each leg paid out, and each venue's round trip, measured live and
drawn on paper. Then how often the two matched the same contracts, live
fewer, or paper fewer, and the newest pairs one by one. Reads only, so it
is safe to run while the live process is writing.

The script sets its own import path, so it runs from any folder.

Run with:
    python3 src/tools/in_play_test.py
    python3 src/tools/in_play_test.py --recent 50
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # src, so the script runs from any folder.
from common.stats import quantile
from common.venues import VENUES
from db.database import read_only
from engine.helper import config
from tools.summary import print_table, short_time

COLUMNS = ("id", "signal_ts", "quantity", "yes_venue", "yes_filled", "yes_latency_ms", "no_venue", "no_filled", "no_latency_ms",
           "matched", "status", "profit", "hedge_pnl", "yes_cost", "no_cost")


def load_pairs(conn):
    """
    Each live trade of the test, oldest first, as (label, live, paper), each trade a dict of COLUMNS with 'settled',
    its result once both legs have paid out, and paper None when it had no twin.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'twins'").fetchone():
        return []
    picked = ", ".join(f"t.{c}" for c in COLUMNS)

    def trade(trade_id):
        if trade_id is None:
            return None
        row = conn.execute(f"""SELECT {picked}, s.yes_payout + s.no_payout FROM trades t
                               LEFT JOIN settlements s ON s.trade_id = t.id WHERE t.id = ?""", (trade_id,)).fetchone()
        t = dict(zip(COLUMNS, row[:-1]))
        payout = row[-1]
        t["settled"] = None if payout is None else payout - t["yes_cost"] - t["no_cost"] + t["hedge_pnl"]
        return t
    rows = conn.execute("""SELECT w.live_trade_id, w.paper_trade_id, p.label FROM twins w
                           JOIN trades t ON t.id = w.live_trade_id JOIN pairs p ON p.id = t.pair_id ORDER BY w.live_trade_id""")
    return [(label, trade(live_id), trade(paper_id)) for live_id, paper_id, label in rows.fetchall()]


def outcome(t):
    """
    How a trade's opening went: 'in full', 'in part', 'one leg', or 'neither'.
    """
    if t["matched"] >= t["quantity"]:
        return "in full"
    if t["matched"]:
        return "in part"
    return "one leg" if t["yes_filled"] or t["no_filled"] else "neither"


def side_by_side(trades):
    """
    The rows comparing one side's trades, live's or paper's: the cells of one column.
    """
    asked, matched = sum(t["quantity"] for t in trades), sum(t["matched"] for t in trades)
    settled = [t["settled"] for t in trades if t["settled"] is not None]
    cells = [len(trades)] + [sum(1 for t in trades if outcome(t) == o) for o in ("in full", "in part", "one leg", "neither")]
    cells += [f"{asked:g}", f"{matched:g} ({100 * matched / asked:.0f}%)" if asked else "0"]
    for venue in VENUES:
        legs = [t[f"{side}_filled"] for t in trades for side in ("yes", "no") if t[f"{side}_venue"] == venue]
        cells.append(f"{sum(1 for filled in legs if filled)} of {len(legs)}")
    cells += [f"{sum(t['profit'] for t in trades):,.2f}", f"{sum(t['hedge_pnl'] for t in trades):+,.2f}",
              f"{sum(settled):+,.2f} ({len(settled)})" if settled else "-"]
    for venue in VENUES:
        ms = [t[f"{side}_latency_ms"] for t in trades for side in ("yes", "no")
              if t[f"{side}_venue"] == venue and t[f"{side}_latency_ms"] is not None]
        cells.append(f"{quantile(ms, 0.5):,.0f} / {quantile(ms, 0.9):,.0f}" if ms else "-")
    return cells


def print_report(conn, recent=20):
    """
    The whole report, see the module's docstring.
    """
    pairs = load_pairs(conn)
    print(f"in-play test: {len(pairs)} of {config.LIVE_IN_PLAY_TRADES} live trades, at most {config.LIVE_IN_PLAY_CONTRACTS} "
          f"contracts each" + (f", {short_time(pairs[0][1]['signal_ts'])} to {short_time(pairs[-1][1]['signal_ts'])} UTC" if pairs else ""))
    twinned = [(label, live, paper) for label, live, paper in pairs if paper]
    if len(twinned) < len(pairs):
        print(f"  {len(pairs) - len(twinned)} without a paper twin, when the paper money fell short, left out below")
    if not twinned:
        return
    names = (["trades", "matched in full", "matched in part", "one leg only", "neither leg", "contracts asked", "contracts matched"]
             + [f"{venue} legs filled" for venue in VENUES]
             + ["locked in $", "sales back $", "settled $ (trades)"]
             + [f"{venue} round trip ms, median / 90th" for venue in VENUES])
    live, paper = side_by_side([p[1] for p in twinned]), side_by_side([p[2] for p in twinned])
    print_table("live beside paper, on the same signals", ("", "live", "paper twin"),
                [(name, a, b) for name, a, b in zip(names, live, paper)])
    fewer = sum(1 for _, a, b in twinned if a["matched"] < b["matched"])
    more = sum(1 for _, a, b in twinned if a["matched"] > b["matched"])
    print(f"\nmatched the same contracts on {len(twinned) - fewer - more} signals of {len(twinned)}, "
          f"live fewer than paper on {fewer}, live more on {more}")
    body = [(short_time(a["signal_ts"])[11:], label[:48], f"{a['yes_filled']:g}/{a['no_filled']:g} of {a['quantity']:g}",
             f"{b['yes_filled']:g}/{b['no_filled']:g}", f"{a['profit'] + a['hedge_pnl']:+.2f}", f"{b['profit'] + b['hedge_pnl']:+.2f}")
            for label, a, b in reversed(twinned[-recent:])]
    print_table(f"the newest {len(body)}, yes/no legs filled, and locked in plus sales back",
                ("time", "bet", "live filled", "paper filled", "live $", "paper $"), body, left=2)


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Compare the in-play test's live trades with their paper twins.")
    ap.add_argument("--recent", type=int, default=20, help="how many of the newest pairs to list one by one")
    args = ap.parse_args()
    print_report(read_only(), args.recent)
