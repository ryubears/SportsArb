"""
Tests for the database report, run against a small database.
"""

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
    ro = database.read_only(path)      # As the script opens it.
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
    assert [row[:3] for row in table(out, "live game opportunities, by kind, last 12 hours")] == [["nfl", "winner", "2"]]


def test_live_game_episodes_are_shown_apart_from_the_rest_with_those_at_the_minimum_edge(tmp_path, monkeypatch, capsys):
    def episode(start, live, days, edge, stretch):
        seconds, size, profit = stretch
        return Opportunity(pair_id=1, trade="t", yes_venue="kalshi", yes_contract="k", no_venue="polymarket_us", no_contract="pm",
                           start_ts=start, end_ts=start, seconds=600, peak_ts=start, peak_edge=edge, peak_size=100, peak_profit=100 * edge,
                           live=live, days_held=days, return_pct=100 * edge / (1 - edge), annual_pct=100 * edge / (1 - edge) * 365 / days,
                           min_edge_seconds=seconds, min_edge_size=size, min_edge_profit=profit)

    def fill(conn):
        database.insert_opportunities(conn, [
            episode("2026-09-27T13:00:00+00:00", 1, 0.1, 0.08, (0.2, 100, 8.0)),     # In a game, gone in 0.2s.
            episode("2026-09-27T14:00:00+00:00", 0, 120, 0.09, (3600.0, 40, 3.6)),   # A future, 40 contracts at 5c+ for an hour.
            episode("2026-09-27T15:00:00+00:00", 0, 120, 0.04, (0.0, 0, 0.0))])      # A future never at 5c.
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert "live game opportunities: 1 episodes in the last 12 hours" in out
    [row] = table(out, "before game/futures opportunities, by kind, last 12 hours")
    assert len(row) == 9 and row[:3] == ["nfl", "winner", "2"]         # No columns for the edge at 5c.
    # The game's 8 cents for 0.2s on 100 contracts, which cost 92$ with fees and lock in 8$.
    assert table(out, "live game opportunities at 5c+, by kind, last 12 hours") == [["nfl", "winner", "1", "0.2", "92", "8.00", "8.7", "31,739.1", "0.1"]]
    # The future's 40 contracts at 91 cents less the 3.60$ they lock in, 9.9% on 36.40$, 30.1% a year over 120 days. The future
    # whose edge never reached 5 cents is left out.
    assert ("before game/futures opportunities at 5c+: 1 episodes in the last 12 hours could have taken 36$ and locked in 3.60$, "
            "9.9% on capital, 30.1% a year, held 120.0 days on average") in out
    assert table(out, "before game/futures opportunities at 5c+, largest, last 12 hours") == [
        ["the", "bet", "t", "9.0", "3,600.0", "40.0", "36", "3.60", "9.9", "30.1", "120.0"]]
    # 30.1% a year clears the 30% the rules ask, so the future is within them too.
    assert ("before game/futures opportunities within the rules: 1 episodes in the last 12 hours could have taken 36$ and locked in "
            "3.60$, 9.9% on capital, 30.1% a year, held 120.0 days on average") in out
    assert "Live game opportunities are not traded: a game is traded only before it kicks off." in out


def test_before_game_episodes_within_the_rules_pay_a_day_or_more_out_and_enough_a_year(tmp_path, monkeypatch, capsys):
    def episode(start, days, edge, size):
        return Opportunity(pair_id=1, trade="t", yes_venue="kalshi", yes_contract="k", no_venue="polymarket_us", no_contract="pm",
                           start_ts=start, end_ts=start, seconds=600, peak_ts=start, peak_edge=edge, peak_size=size, peak_profit=size * edge,
                           live=0, days_held=days, return_pct=100 * edge / (1 - edge), annual_pct=100 * edge / (1 - edge) * 365 / days,
                           min_edge_seconds=600.0, min_edge_size=size, min_edge_profit=size * edge)

    def fill(conn):
        database.insert_opportunities(conn, [
            episode("2026-09-27T13:00:00+00:00", 2, 0.06, 100),       # 6 cents paying in two days, 1,165% a year.
            episode("2026-09-27T14:00:00+00:00", 0.5, 0.10, 100),     # Pays out in 12 hours, too soon.
            episode("2026-09-27T15:00:00+00:00", 150, 0.10, 100)])    # 10 cents over 150 days, 27% a year, too little.
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert "before game/futures opportunities at 5c+: 3 episodes" in out
    assert ("before game/futures opportunities within the rules: 1 episodes in the last 12 hours could have taken 94$ and locked in "
            "6.00$, 6.4% on capital, 1,164.9% a year, held 2.0 days on average") in out


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


def test_live_money_shows_each_venue_read_now_with_the_kalshi_shards(capsys):
    def unreachable():
        raise RuntimeError("401 unauthorized")

    balances = summary.read_live_balances({"kalshi": lambda: (92.0, {0: 0.0, 1: 0.0, 2: 0.0, 3: 92.0}), "polymarket_us": unreachable})
    assert balances == {"kalshi": (92.0, {0: 0.0, 1: 0.0, 2: 0.0, 3: 92.0}), "polymarket_us": "not read (401 unauthorized)"}
    summary.print_live_money(balances)
    # The shards live trading uses, and any other holding money.
    assert capsys.readouterr().out.splitlines()[1:] == [
        "live money",
        "  live balances on the venues, read now: kalshi 92.00$ (shard 0 0.00$, shard 3 92.00$), polymarket_us not read (401 unauthorized)",
    ]


def test_open_trades_show_what_they_hold_and_when_it_comes_back(tmp_path, monkeypatch, capsys):
    def fill(conn):
        for held, cost, pays_at in ((5, 2.5, "2026-12-20T00:00:00+00:00"), (5, 3.0, "2026-10-04T20:45:00+00:00"), (0, 0.0, INSIDE)):
            database.insert_trade(conn, Trade(mode="live", pair_id=1, trade="t", signal_ts=INSIDE, edge=0.06, quantity=5,
                                              yes_venue="kalshi", yes_contract="k", yes_polarity="yes", yes_limit=0.5,
                                              no_venue="polymarket_us", no_contract="p", no_polarity="yes", no_limit=0.45,
                                              pays_at=pays_at, yes_held=held, no_held=held, yes_cost=cost, no_cost=cost * 0.9, status="filled"))
    out = report(tmp_path, monkeypatch, capsys, fill)
    # The third was flattened to nothing, so holds nothing.
    assert "  2 live trades still open, holding kalshi 5.50$, polymarket_us 4.95$, paying out from 2026-10-04 to 2026-12-20" in out
    # Held 83.2, 7.0, and 0 days from the signal to the payout, 30.1 on average.
    assert table(out, "live by kind, last 12 hours")[0][:7] == ["nfl", "winner", "3", "6.0", "0.0", "0.0", "30.1"]
