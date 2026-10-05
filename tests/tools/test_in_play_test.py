"""
Tests for the report on the in-play test's live trades beside their paper twins.
"""

import re
from db import database
from db.models import Settlement, Trade
from tools import in_play_test

SIGNAL = "2026-10-05T17:01:14.500000+00:00"


def trade(conn, mode, yes_filled, no_filled, yes_ms, no_ms, profit=0.0, hedge=0.0):
    """
    Store a trade of 5 contracts with yes on Polymarket US and no on Kalshi, filled as given, and return its id.
    """
    matched = min(yes_filled, no_filled)
    t = Trade(mode=mode, pair_id=1, trade="t", signal_ts=SIGNAL, edge=0.03, quantity=5, pays_at="2026-10-05T21:00:00+00:00",
              yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45, yes_filled=yes_filled,
              yes_latency_ms=yes_ms, no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.52, no_filled=no_filled,
              no_latency_ms=no_ms, matched=matched, yes_held=matched, no_held=matched, yes_cost=0.45 * matched,
              no_cost=0.52 * matched, profit=profit, hedge_pnl=hedge,
              status="filled" if matched == 5 else "partial" if matched else "failed")
    return database.insert_trade(conn, t)


def test_the_live_trades_are_set_beside_their_paper_twins_for_each_order_of_their_orders(tmp_path, capsys):
    conn = database.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO pairs (id, sport, label, kind, venues, contracts, flags, matched_at) "
                 "VALUES (1, 'nfl', 'nfl spread 2026-10-05 IND@WAS IND 3.5', 'spread', 'kalshi,polymarket_us', 2, '[]', ?)", (SIGNAL,))
    # Both orders at once, and both filled in full. Then Polymarket US first, where live's missed, so its Kalshi order was
    # never sent, while paper's twin filled both. A third live trade found the paper money short.
    pairs = [(trade(conn, "live", 5, 5, 95, 20, profit=0.15), trade(conn, "paper", 5, 5, 90, 20, profit=0.15), "together"),
             (trade(conn, "live", 0, 0, 60, 0), trade(conn, "paper", 5, 5, 88, 19, profit=0.15), "polymarket_first"),
             (trade(conn, "live", 5, 5, 99, 22, profit=0.15), None, "together")]
    conn.executemany("INSERT INTO twins (live_trade_id, paper_trade_id, sequence) VALUES (?, ?, ?)", pairs)
    s = Settlement(pairs[0][0], SIGNAL, mode="live")                    # The first live trade has paid out: yes won.
    s.record("yes", "yes", 5.0, SIGNAL)
    s.record("no", "no", 0.0, SIGNAL)
    database.insert_settlement(conn, s)
    conn.commit()
    in_play_test.print_report(database.read_only(tmp_path / "t.sqlite"))
    out = capsys.readouterr().out
    assert out.startswith("in-play test: 3 of 200 live trades, at most 5 contracts each, 2026-10-05 17:01:14 to 2026-10-05 17:01:14 UTC\n"
                          "  1 without a paper twin, when the paper money fell short, left out below\n")
    table = out.split("\nlive beside paper, on the same signals\n")[1].split("\n\n")[0].splitlines()[1:]    # Past the header.
    rows = {cells[0]: cells[1:] for cells in (re.split(r"\s{2,}", line.strip()) for line in table)}
    # Sent at once, then Polymarket US first: live and paper each.
    assert rows["trades"] == ["1", "1", "1", "1"]
    assert rows["matched in full"] == ["1", "1", "0", "1"] and rows["neither leg"] == ["0", "0", "1", "0"]
    assert rows["contracts matched"] == ["5 (100%)", "5 (100%)", "0 (0%)", "5 (100%)"]
    assert rows["polymarket_us legs filled, of those sent"] == ["1 of 1", "1 of 1", "0 of 1", "1 of 1"]
    assert rows["kalshi legs filled, of those sent"] == ["1 of 1", "1 of 1", "0 of 0", "1 of 1"]       # Live's never went.
    assert rows["locked in $"] == ["0.15", "0.15", "0.00", "0.15"] and rows["sales back $"] == ["+0.00"] * 4
    # The settled live trade paid 5 on yes against the 2.25 + 2.60 its legs cost.
    assert rows["settled $ (trades)"] == ["+0.15 (1)", "-", "-", "-"]
    assert rows["kalshi round trip ms, median / 90th"] == ["20 / 20", "20 / 20", "-", "19 / 19"]
    assert ("both at once: matched the same contracts on 1 signals of 1, live fewer than paper on 0, live more on 0\n"
            "Polymarket US first: matched the same contracts on 0 signals of 1, live fewer than paper on 1, live more on 0") in out
    newest = out.split("legs filled, and locked in plus sales back\n")[1].splitlines()[1].split()
    assert newest[:2] == ["17:01:14", "nfl"] and newest[-8:] == ["PM", "first", "0/0", "of", "5", "5/5", "+0.00", "+0.15"]


def test_a_database_without_the_test_says_it_has_no_trades(tmp_path, capsys):
    path = tmp_path / "t.sqlite"
    database.connect(path).execute("DROP TABLE twins")
    in_play_test.print_report(database.read_only(path))
    assert capsys.readouterr().out == "in-play test: 0 of 200 live trades, at most 5 contracts each\n"
