"""
Tests for the database report, run against a small database.
"""

import sqlite3
from db import database
from db.models import Opportunity, Settlement, Trade
from tools import summary

SINCE = "2026-09-27T12:00:00+00:00"     # Where the report's window starts.
BEFORE = "2026-09-27T11:00:00+00:00"    # An hour before the window.
INSIDE = "2026-09-27T20:00:00+00:00"    # Eight hours into it.


def report(tmp_path, monkeypatch, capsys, fill):
    """
    What the report prints for the 12 hours from SINCE, on a database with one pair that fill(conn) adds to.
    """
    path = tmp_path / "t.sqlite"
    conn = database.connect(path)
    conn.execute("INSERT INTO pairs (id, label, kind, venues, contracts, flags, matched_at) "
                 "VALUES (1, 'the bet', 'winner', 'kalshi,polymarket_us', 2, '[]', ?)", (BEFORE,))
    fill(conn)
    conn.commit()
    monkeypatch.setattr(summary, "DB_PATH", path)
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)      # As the script opens it.
    summary.print_storage(ro)
    summary.print_pairs(ro)
    summary.print_opportunities(ro, SINCE, 12)
    summary.print_trades(ro, SINCE, 12)
    return capsys.readouterr().out


def table(out, title):
    """
    The rows of the table printed under title, each split into its cells:
    the lines after its header with as many cells as the first of them.
    """
    rows = []
    for line in out.split(f"\n{title}\n")[1].splitlines()[1:]:     # The header left out.
        cells = line.split()
        if not cells or (rows and len(cells) != len(rows[0])):
            break
        rows.append(cells)
    return rows


def test_episodes_are_covered_from_the_first_start_to_the_newest_end(tmp_path, monkeypatch, capsys):
    def fill(conn):
        episodes = [("2026-09-27T13:00:00+00:00", "2026-09-27T13:05:00+00:00"),      # Stored first, as it ended first.
                    ("2026-09-27T13:30:00+00:00", "2026-09-27T15:00:00+00:00")]
        database.insert_opportunities(conn, [
            Opportunity(pair_id=1, trade="t", yes_venue="kalshi", yes_contract="k", no_venue="polymarket_us", no_contract="pm",
                        start_ts=start, end_ts=end, seconds=60, peak_ts=start, peak_edge=0.05, peak_size=10, peak_profit=0.5,
                        live=1, days_held=0.1, return_pct=5, annual_pct=50) for start, end in episodes])
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert "2 episodes in all, covering 2026-09-27 13:00:00 to 2026-09-27 15:00:00 UTC, 2 in the last 12 hours" in out
    assert [row[:3] for row in table(out, "by kind, last 12 hours")] == [["nfl", "winner", "2"]]


def test_settled_legs_count_by_when_each_leg_settled(tmp_path, monkeypatch, capsys):
    def fill(conn):
        for yes_at, no_at in ((BEFORE, INSIDE), (BEFORE, BEFORE)):
            t = Trade(mode="paper", pair_id=1, trade="t", signal_ts=BEFORE, edge=0.08, quantity=10,
                      yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45,
                      no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.47, pays_at=BEFORE,
                      yes_held=10, no_held=10, yes_cost=4.5, no_cost=4.7, status="filled")
            database.insert_trade(conn, t)
            s = Settlement(t.id, max(yes_at, no_at), mode="paper")
            s.record("yes", "yes", 10.0, yes_at)
            s.record("no", "no", 0.0, no_at)
            database.insert_settlement(conn, s)
    out = report(tmp_path, monkeypatch, capsys, fill)
    # Only the first trade's no leg, on Kalshi, settled in the window. Its yes leg settled before it, as did the second trade.
    assert table(out, "paper settled legs by venue, last 12 hours") == [["kalshi", "1", "10", "4.7", "0.0", "-4.7"]]
