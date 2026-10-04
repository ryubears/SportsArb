"""
Tests for the database report, run against a small database.
"""

from db import database
from db.models import Opportunity, Settlement, Trade
from tools import summary

SINCE = "2026-09-27T12:00:00+00:00"     # Where the report's window starts.
BEFORE = "2026-09-27T11:00:00+00:00"    # An hour before the window.
INSIDE = "2026-09-27T20:00:00+00:00"    # Eight hours into it.


NOW = "2026-09-28T00:00:00+00:00"       # When the report runs, twelve hours into the window.


def report(tmp_path, monkeypatch, capsys, fill, modes=("live",), sports=()):
    """
    What the report prints for the 12 hours from SINCE, on a database with one nfl pair and one nba pair that fill(conn) adds to.
    """
    path = tmp_path / "t.sqlite"
    conn = database.connect(path)
    conn.execute("INSERT INTO pairs (id, sport, label, kind, venues, contracts, flags, matched_at) "
                 "VALUES (1, 'nfl', 'the bet', 'winner', 'kalshi,polymarket_us', 2, '[]', ?)", (BEFORE,))
    conn.execute("INSERT INTO pairs (id, sport, label, kind, venues, contracts, flags, matched_at) "
                 "VALUES (2, 'nba', 'other bet', 'champion', 'kalshi,polymarket_us', 2, '[]', ?)", (BEFORE,))
    fill(conn)
    conn.commit()
    monkeypatch.setattr(summary, "DB_PATH", path)
    ro = database.read_only(path)      # As the script opens it.
    summary.print_overview(ro, sports)
    summary.print_opportunities(ro, SINCE, 12, sports)
    summary.print_trades(ro, SINCE, 12, modes, sports, now=NOW)
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


def episode(start, days, edge, size, live=0, pair_id=1):
    """
    An episode of the pair at start, paying days out, edge at its peak, and size fillable at 5c or more for ten minutes.
    """
    return Opportunity(pair_id=pair_id, trade="t", yes_venue="kalshi", yes_contract="k", no_venue="polymarket_us", no_contract="pm",
                       start_ts=start, end_ts=start, seconds=600, peak_ts=start, peak_edge=edge, peak_size=size, peak_profit=size * edge,
                       live=live, days_held=days, return_pct=100 * edge / (1 - edge), annual_pct=100 * edge / (1 - edge) * 365 / days,
                       min_edge_seconds=600.0, min_edge_size=size, min_edge_profit=size * edge)


def test_only_episodes_within_the_rules_are_shown(tmp_path, monkeypatch, capsys):
    def fill(conn):
        database.insert_opportunities(conn, [
            episode("2026-09-27T13:00:00+00:00", 2, 0.06, 100),           # 6 cents paying in two days, 1,165% a year.
            episode("2026-09-27T14:00:00+00:00", 0.5, 0.10, 100),         # Pays out in 12 hours, too soon.
            episode("2026-09-27T15:00:00+00:00", 150, 0.10, 100),         # 10 cents over 150 days, 27% a year, too little.
            episode("2026-09-27T16:00:00+00:00", 2, 0.04, 100),           # Under 5 cents.
            episode("2026-09-27T17:00:00+00:00", 2, 0.08, 100, live=1)])  # During a game.
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert ("\nopportunities within the rules (5c+, paying 24h+ out, 50%+ a year), last 12 hours\n"
            "  1 episodes could have taken 94$ and locked in 6.00$\n"
            "  6.4% on capital, 1,164.9% a year, held 2.0 days on average\n") in out
    assert table(out, "by kind") == [["nfl", "winner", "1", "600.0", "94", "6.00", "6.4", "1,164.9", "2.0"]]
    assert table(out, "largest") == [["the", "bet", "6.0", "600", "100.0", "94", "6.00", "1,164.9", "2.0"]]


def test_a_sport_filter_keeps_only_its_pairs_episodes_and_trades(tmp_path, monkeypatch, capsys):
    def fill(conn):
        database.insert_opportunities(conn, [episode("2026-09-27T13:00:00+00:00", 2, 0.06, 100),
                                             episode("2026-09-27T14:00:00+00:00", 2, 0.07, 50, pair_id=2)])
    out = report(tmp_path, monkeypatch, capsys, fill, sports=("nba",))
    assert "  1 episodes could have taken" in out and [r[:2] for r in table(out, "by kind")] == [["nba", "champion"]]
    assert "\nlive trades: 0 in all, 0 in the last 12 hours" in out


def test_all_modes_show_paper_then_live(tmp_path, monkeypatch, capsys):
    out = report(tmp_path, monkeypatch, capsys, lambda conn: None, modes=summary.MODES["all"])
    assert out.index("\npaper trades: ") < out.index("\npaper open trades: ") < out.index("\nlive trades: ") < out.index("\nlive open trades: ")


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
    out = report(tmp_path, monkeypatch, capsys, fill, modes=("paper",))
    # Only the first trade's no leg, on Kalshi, settled in the window. Its yes leg settled before it, as did the second trade.
    assert table(out, "paper settled legs by venue, last 12 hours") == [["kalshi", "1", "10", "4.7", "0.0", "-4.7"]]


def test_live_money_shows_each_venue_read_now_with_the_kalshi_shards(capsys):
    def unreachable():
        raise RuntimeError("401 unauthorized")

    balances = summary.read_live_balances({"kalshi": lambda: (92.0, {0: 0.0, 1: 0.0, 2: 0.0, 3: 92.0}), "polymarket_us": unreachable})
    assert balances == {"kalshi": (92.0, {0: 0.0, 1: 0.0, 2: 0.0, 3: 92.0}), "polymarket_us": "not read (401 unauthorized)"}
    summary.print_live_money(balances)
    # The shards live trading uses, and any other holding money.
    assert capsys.readouterr().out.splitlines()[1:] == [
        "live money on the venues, read now", "  kalshi 92.00$ (shard 0 0.00$, shard 3 92.00$)", "  polymarket_us not read (401 unauthorized)"]


def open_trades(conn, mode="live", pair_id=1):
    """
    Add open trades of the mode on the pair: (held a side, yes cost, no cost, locked in, flattening, signal, pays at).
    One opened half an hour before NOW settling in December, one opened ten hours before it paying on September 27,
    already past and waiting on a venue, one opened two days before it paying October 6, one opened ten days before it
    paying October 4 that lost 2 cents flattening, and one in the window flattened to nothing, which holds nothing.
    """
    for held, yes_cost, no_cost, profit, hedge, signal, pays_at in (
            (5, 2.5, 2.25, 0.25, 0.0, "2026-09-27T23:30:00+00:00", "2026-12-20T00:00:00+00:00"),
            (5, 2.0, 2.7, 0.30, 0.0, "2026-09-27T14:00:00+00:00", "2026-09-27T20:00:00+00:00"),
            (5, 2.0, 2.7, 0.30, 0.0, "2026-09-26T00:00:00+00:00", "2026-10-06T00:00:00+00:00"),
            (5, 3.0, 1.7, 0.30, -0.02, "2026-09-18T00:00:00+00:00", "2026-10-04T20:45:00+00:00"),
            (0, 0.0, 0.0, 0.0, -0.05, INSIDE, INSIDE)):
        database.insert_trade(conn, Trade(mode=mode, pair_id=pair_id, trade="t", signal_ts=signal, edge=0.06, quantity=5,
                                          yes_venue="kalshi", yes_contract="k", yes_polarity="yes", yes_limit=0.5,
                                          no_venue="polymarket_us", no_contract="p", no_polarity="yes", no_limit=0.45,
                                          pays_at=pays_at, yes_held=held, no_held=held, yes_cost=yes_cost, no_cost=no_cost,
                                          profit=profit, hedge_pnl=hedge, status="filled"))


def test_open_trades_are_summed_up_with_when_they_opened_and_resolve(tmp_path, monkeypatch, capsys):
    out = report(tmp_path, monkeypatch, capsys, open_trades)
    lines = out.split("\nlive open trades: ")[1].splitlines()
    # The capital of each window is what its trades still hold.
    assert lines[:4] == ["4",
                         "  opened in the last hour  1 using  4.75$",
                         "  opened in the last day   2 using  9.45$",
                         "  opened in the last week  3 using 14.15$"]
    assert lines[4] == "  capital 18.85$, held on kalshi 9.50$, polymarket_us 9.35$"
    # 1.13$ on 18.85$ is 6.0%. A year's rate weighted by capital: 23.1% on 4.75$ over 83 days, 9,319% on 4.70$ over
    # 0.25 days, 233% on 4.70$ over 10 days, and 129% on 4.70$ over 16.9 days.
    assert lines[5] == "  expected to return 1.13$ in profit, 6.0% on capital, 2,419.7% a year"
    # The average is weighted by capital: about a quarter of it on each date, which comes to October 22.
    assert lines[6] == "  resolving first 2026-09-27, on average 2026-10-22, last 2026-12-20"
    # Each one opened in the 12 hours of the window, newest first. The one flattened to nothing is not open.
    assert table(out, "live open trades opened in the last 12 hours, newest first") == [
        ["1", "the", "bet", "2026-09-27", "23:30", "5/5", "4.75", "0.25", "5.3", "23.1", "2026-12-20"],
        ["2", "the", "bet", "2026-09-27", "14:00", "5/5", "4.70", "0.30", "6.4", "9,319.1", "2026-09-27"]]


def test_open_trades_keep_to_the_mode_and_sports(tmp_path, monkeypatch, capsys):
    def fill(conn):
        open_trades(conn, "paper", pair_id=1)
        open_trades(conn, "live", pair_id=2)
    out = report(tmp_path, monkeypatch, capsys, fill, modes=summary.MODES["all"], sports=("nba",))
    assert "\npaper open trades: none" in out
    assert out.split("\nlive open trades: ")[1].startswith("4\n")
    assert [r[:3] for r in table(out, "live open trades opened in the last 12 hours, newest first")] == [["6", "other", "bet"], ["7", "other", "bet"]]


def test_open_trades_none_opened_in_the_window_say_so(tmp_path, monkeypatch, capsys):
    out = report(tmp_path, monkeypatch, capsys, lambda conn: open_trades(conn))
    assert "  none opened in the last" not in out
    out = report(tmp_path / "b", monkeypatch, capsys, lambda conn: database.insert_trade(conn, Trade(
        mode="live", pair_id=1, trade="t", signal_ts=BEFORE, edge=0.06, quantity=5, yes_venue="kalshi", yes_contract="k",
        yes_polarity="yes", yes_limit=0.5, no_venue="polymarket_us", no_contract="p", no_polarity="yes", no_limit=0.45,
        pays_at="2026-12-20T00:00:00+00:00", yes_held=5, no_held=5, yes_cost=2.5, no_cost=2.25, profit=0.25, status="filled")))
    assert out.split("\nlive open trades: ")[1].splitlines()[7] == "  none opened in the last 12 hours"


def test_contract_counts_show_to_the_hundredth_without_float_noise():
    # Matched contracts summed to 0.21000000000000002 in the live outcomes on 2026-10-02.
    assert [summary.contracts(v) for v in (0.21000000000000002, 284532, 630.0, 13.6, None)] == ["0.21", "284,532", "630", "13.6", "0"]


def test_a_long_list_wraps_onto_indented_lines_without_splitting_an_item(capsys):
    sports = [f"sport{i} {i * 1000:,}" for i in range(20)]
    summary.print_listed("5,365 pairs, last matched 2026-10-04 01:12:45 UTC", sports)
    summary.print_listed("feed drops, last 24 hours", [])
    lines = capsys.readouterr().out.splitlines()
    wrapped = lines[1:lines.index("feed drops, last 24 hours")]
    assert lines[0] == "5,365 pairs, last matched 2026-10-04 01:12:45 UTC" and len(wrapped) > 1
    assert all(line.startswith("  ") and len(line) <= summary.WIDTH for line in wrapped)
    assert " ".join(line.strip() for line in wrapped) == ", ".join(sports)
    assert lines[-1] == "  none"
