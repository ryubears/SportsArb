"""
Round trip every table through the database module.
"""

from db import database
from db.models import Bet, Contract, Gap, Opportunity, Pair, Quote


def contract(venue, contract_id, **fields):
    c = Contract(venue=venue, contract_id=contract_id, market_id=contract_id, event_id="ev", series_id=None,
                 sport="nfl", event_title=None, title="t", outcome="Yes", market_type=None, line=None, rules=None,
                 start_time=None, close_time="2027-01-01T00:00:00+00:00", fee_info={"rate": 0.05})
    for k, v in fields.items():
        setattr(c, k, v)
    return c


def test_contracts_upsert_keeps_first_seen(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.upsert_contracts(conn, [contract("kalshi", "A")], "2026-01-01T00:00:00+00:00")
    database.upsert_contracts(conn, [contract("kalshi", "A", title="renamed")], "2026-01-02T00:00:00+00:00")
    rows = database.load_contracts(conn, sport="nfl")
    assert len(rows) == 1
    assert (rows[0]["title"], rows[0]["first_seen"][:10], rows[0]["last_seen"][:10]) == ("renamed", "2026-01-01", "2026-01-02")


def test_bets_pairs_quotes_and_targets(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.upsert_contracts(conn, [contract("polymarket_us", "pm"), contract("kalshi", "k")], "2026-01-01T00:00:00+00:00")
    bets = [Bet(v, cid, "champion", 2027, None, None, None, "BUF", None, "yes") for v, cid in (("polymarket_us", "pm"), ("kalshi", "k"))]
    database.replace_bets(conn, "nfl", bets)
    assert {b["venue"] for b in database.load_bets(conn, "nfl")} == {"polymarket_us", "kalshi"}

    g = Pair("champion 2027 BUF", "champion", 2027, None, None, None, "BUF", None, bets, ["note"])
    database.replace_pairs(conn, "nfl", [g], "2026-01-01T00:00:00+00:00")
    pairs = database.load_pairs(conn, "nfl")
    assert list(pairs) == [g.id] and g.id == 1 and [m.pair_id for m in g.members] == [1, 1]
    assert (pairs[1]["label"], pairs[1]["venues"]) == ("champion 2027 BUF", "kalshi,polymarket_us")
    assert sorted(m["contract_id"] for m in pairs[1]["members"]) == ["k", "pm"]
    assert pairs[1]["members"][0]["close_time"] == "2027-01-01T00:00:00+00:00"
    database.replace_pairs(conn, "nfl", [g], "2026-01-02T00:00:00+00:00")
    assert g.id == 1 and tuple(conn.execute("SELECT COUNT(*), MAX(matched_at) FROM pairs").fetchone()) == (1, "2026-01-02T00:00:00+00:00")   # The same pair keeps its id.

    venues = ["polymarket_us", "kalshi"]
    targets = database.load_recording_targets(conn, "nfl", "2026-06-01T00:00:00+00:00", "2026-06-08T00:00:00+00:00", venues, "2026-05-31T19:00:00+00:00")
    assert targets == {"polymarket_us": ["pm"], "kalshi": ["k"]}
    closed = database.load_recording_targets(conn, "nfl", "2028-01-01T00:00:00+00:00", "2028-01-08T00:00:00+00:00", venues, "2027-12-31T19:00:00+00:00")
    assert closed == {"polymarket_us": [], "kalshi": []}


def test_game_contracts_stay_targets_through_the_game(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    # A venue may close its game markets at kickoff on paper while they trade through the game.
    game = dict(start_time="2026-09-20T17:00:00+00:00", close_time="2026-09-20T17:00:00+00:00")
    database.upsert_contracts(conn, [contract("polymarket_us", "pm", **game), contract("kalshi", "k", **game)], "2026-01-01T00:00:00+00:00")
    bets = [Bet(v, cid, "game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, "yes") for v, cid in (("polymarket_us", "pm"), ("kalshi", "k"))]
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", [Pair("game_winner 2026-09-20 CAR@ATL CAR", "game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, bets, [])], "2026-01-01T00:00:00+00:00")
    during = database.load_recording_targets(conn, "nfl", "2026-09-20T18:30:00+00:00", "2026-09-27T18:30:00+00:00", ["polymarket_us"], "2026-09-20T13:30:00+00:00")
    after = database.load_recording_targets(conn, "nfl", "2026-09-21T00:00:00+00:00", "2026-09-28T00:00:00+00:00", ["polymarket_us"], "2026-09-20T19:00:00+00:00")
    assert during == {"polymarket_us": ["pm"]}
    assert after == {"polymarket_us": []}

    database.insert_quotes(conn, [Quote("kalshi", "k", "2026-01-01T00:00:00+00:00", [[0.5, 1]], [[0.6, 2]])])
    q = database.load_quotes(conn, "kalshi", ["k"])["k"][0]
    assert (q.bids, q.asks) == ([[0.5, 1]], [[0.6, 2]])
    assert database.load_quotes(conn, "kalshi", ["k"], since="2026-02-01")["k"] == []


def test_gaps_are_stored_in_time_order_and_filtered_by_since(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.insert_gap(conn, Gap("polymarket_us", "2026-09-20T20:39:07+00:00", "2026-09-20T20:39:12+00:00"))
    database.insert_gap(conn, Gap("polymarket_us", "2026-09-20T20:37:31+00:00", None))
    assert [g.start_ts[11:19] for g in database.load_gaps(conn, "polymarket_us")] == ["20:37:31", "20:39:07"]
    assert [g.end_ts for g in database.load_gaps(conn, "polymarket_us", "2026-09-20T20:38:00+00:00")] == ["2026-09-20T20:39:12+00:00"]
    assert database.load_gaps(conn, "kalshi") == []


def test_opportunities_append_and_an_old_source_column_is_dropped(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    o = Opportunity(1, "yes: K buy, no: PMUS buy", "kalshi", "k", "polymarket_us", "pm", "t0", "t1", 60, "t0", 0.02, 100, 2.0, 0, 10.0, 2.04, 74.5)
    database.insert_opportunities(conn, [o])
    database.insert_opportunities(conn, [o])
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2
    conn.execute("ALTER TABLE opportunities ADD COLUMN source TEXT")      # As the retired replay scanner left it,
    conn.execute("PRAGMA user_version = 0")                             # before migrations were numbered.
    conn.commit()
    conn = database.connect(tmp_path / "t.sqlite")
    assert "source" not in [r[1] for r in conn.execute("PRAGMA table_info(opportunities)")]
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2


def test_replace_pairs_unlinks_pairs_that_disappeared_but_keeps_their_rows(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.upsert_contracts(conn, [contract("polymarket_us", "pm"), contract("kalshi", "k")], "2026-01-01T00:00:00+00:00")
    bets = [Bet(v, cid, "champion", 2027, None, None, None, "BUF", None, "yes") for v, cid in (("polymarket_us", "pm"), ("kalshi", "k"))]
    database.replace_bets(conn, "nfl", bets)
    g = Pair("champion 2027 BUF", "champion", 2027, None, None, None, "BUF", None, bets, [])
    database.replace_pairs(conn, "nfl", [g], "2026-01-01T00:00:00+00:00")
    database.replace_pairs(conn, "nfl", [], "2026-01-02T00:00:00+00:00")
    assert database.load_pairs(conn, "nfl") == {}
    assert conn.execute("SELECT COUNT(*) FROM bets WHERE pair_id IS NOT NULL").fetchone()[0] == 0
    assert [tuple(r) for r in conn.execute("SELECT id, label FROM pairs")] == [(1, "champion 2027 BUF")]       # Trades may still refer to it.


def test_old_databases_are_migrated_to_pairs(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE bets (venue TEXT, contract_id TEXT, kind TEXT, season INTEGER, game_date TEXT, team_a TEXT, team_b TEXT,
                           subject TEXT, line REAL, polarity TEXT, group_label TEXT, PRIMARY KEY (venue, contract_id));
        INSERT INTO bets VALUES ('kalshi', 'k', 'champion', 2027, NULL, NULL, NULL, 'BUF', NULL, 'yes', 'champion 2027 BUF');
        CREATE TABLE bet_groups (label TEXT PRIMARY KEY);
        CREATE TABLE fee_history (venue TEXT, contract_id TEXT, seen_at TEXT, fee_info TEXT);
        CREATE TABLE stream_gaps (venue TEXT, start_ts TEXT, end_ts TEXT, PRIMARY KEY (venue, start_ts));
        INSERT INTO stream_gaps VALUES ('kalshi', '2026-09-21T19:39:39+00:00', '2026-09-21T19:39:41+00:00');
        CREATE TABLE ledger (id INTEGER PRIMARY KEY, ts TEXT, venue TEXT, amount REAL, reason TEXT, trade_id INTEGER);
        INSERT INTO ledger (ts, venue, amount, reason, trade_id) VALUES ('t1', 'kalshi', -23.5, 'buy', 1), ('t2', 'kalshi', 50, 'payout', 1);
    """)
    old.commit(); old.close()
    conn = database.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "pairs" in tables and "gaps" in tables
    assert not {"bet_groups", "fee_history", "stream_gaps"} & tables
    assert "pair_label" not in [r[1] for r in conn.execute("PRAGMA table_info(bets)")]
    assert conn.execute("SELECT pair_id FROM bets").fetchone()[0] is None      # No pairs table yet, the next match sets it.
    assert [g.start_ts[11:19] for g in database.load_gaps(conn, "kalshi")] == ["19:39:39"]
    # Replayed from the start, then opened with the starting balance that the first entry implies.
    assert [tuple(r) for r in conn.execute("SELECT ts, amount, reason, balance FROM ledger ORDER BY id")] == [
        ("t1", 10000, "transfer_in", 10000), ("t1", -23.5, "buy", 10000 - 23.5), ("t2", 50, "payout", 10000 + 26.5)]
    assert database.last_balances(conn) == {"kalshi": 10026.5}


def test_old_settlement_rows_are_folded_into_their_trades(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE pairs (label TEXT PRIMARY KEY, kind TEXT NOT NULL, season INTEGER, game_date TEXT, team_a TEXT, team_b TEXT, subject TEXT,
                            line REAL, venues TEXT NOT NULL, contracts INTEGER NOT NULL, flags TEXT NOT NULL, matched_at TEXT NOT NULL);
        INSERT INTO pairs VALUES ('spread 2026-09-27 KC@MIA KC 30.5', 'spread', 2027, '2026-09-27', 'KC', 'MIA', 'KC', 30.5, 'kalshi,polymarket_us', 2, '[]', 'm');
        CREATE TABLE bets (venue TEXT, contract_id TEXT, kind TEXT, season INTEGER, game_date TEXT, team_a TEXT, team_b TEXT,
                           subject TEXT, line REAL, polarity TEXT, pair_label TEXT, PRIMARY KEY (venue, contract_id));
        INSERT INTO bets VALUES ('kalshi', 'k', 'spread', 2027, '2026-09-27', 'KC', 'MIA', 'KC', 30.5, 'yes', 'spread 2026-09-27 KC@MIA KC 30.5');
        CREATE TABLE opportunities (label TEXT, kind TEXT, trade TEXT, yes_venue TEXT, yes_contract TEXT, no_venue TEXT, no_contract TEXT,
            start_ts TEXT, end_ts TEXT, seconds REAL, peak_ts TEXT, peak_edge REAL, peak_size REAL, peak_profit REAL, live INTEGER,
            days_held REAL, return_pct REAL, annual_pct REAL);
        INSERT INTO opportunities VALUES ('spread 2026-09-27 KC@MIA KC 30.5', 'spread', 't', 'kalshi', 'k', 'polymarket_us', 'pm', 's', 'e', 60, 's', 0.005, 1000, 5, 0, 3, 0.5, 55),
                                         ('l', 'spread', 't', 'kalshi', 'k', 'polymarket_us', 'pm', 'gone', 'e', 60, 's', 0.005, 1000, 5, 0, 3, 0.5, 55);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, label TEXT, kind TEXT, trade TEXT, signal_ts TEXT, edge REAL, quantity INTEGER,
            yes_venue TEXT, yes_contract TEXT, yes_polarity TEXT, yes_limit REAL, yes_filled INTEGER, yes_cost REAL, yes_latency_ms INTEGER, yes_fill_ts TEXT,
            no_venue TEXT, no_contract TEXT, no_polarity TEXT, no_limit REAL, no_filled INTEGER, no_cost REAL, no_latency_ms INTEGER, no_fill_ts TEXT,
            yes_held INTEGER, no_held INTEGER, matched INTEGER, profit REAL, hedge TEXT, hedge_pnl REAL, status TEXT, pays_at TEXT, settled_at TEXT);
        INSERT INTO trades VALUES (1, 'l', 'spread', 't', 's', 0.02, 5, 'polymarket_us', 'pm', 'yes', 0.45, 5, 2.25, 50, 'f', 'kalshi', 'k', 'yes', 0.47, 5, 2.35, 50, 'f',
            5, 5, 5, 0.4, 'none', 0, 'filled', 'p', '2026-09-20T20:10:00+00:00');
        CREATE TABLE settlements (trade_id INTEGER, venue TEXT, contract_id TEXT, side TEXT, held INTEGER, cost REAL, result TEXT, payout REAL, realized REAL, settled_at TEXT);
        INSERT INTO settlements VALUES (1, 'polymarket_us', 'pm', 'yes', 5, 2.25, 'yes', 5, 2.75, '2026-09-20T20:10:00+00:00'),
                                       (1, 'kalshi', 'k', 'no', 5, 2.35, 'yes', 0, -2.35, '2026-09-20T20:09:00+00:00');
    """)
    old.commit(); old.close()
    conn = database.connect(path)
    # The legs were folded into the trade, then moved to the settlements table of today, one row for the trade.
    assert "side" not in [r[1] for r in conn.execute("PRAGMA table_info(settlements)")]
    assert "settled_at" not in [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    row = conn.execute("SELECT trade_id, yes_result, yes_payout, yes_settled_at, no_result, no_payout, no_settled_at, settled_at FROM settlements").fetchone()
    assert tuple(row) == (1, "yes", 5, "2026-09-20T20:10:00+00:00", "yes", 0, "2026-09-20T20:09:00+00:00", "2026-09-20T20:10:00+00:00")
    assert database.load_open_trades(conn, "paper") == []
    # Pairs got ids. The stored pair kept its row, the trade's and the second episode's pairs, long gone from the catalog, got bare rows.
    assert [tuple(r) for r in conn.execute("SELECT id, label, contracts FROM pairs ORDER BY id")] == [
        (1, "spread 2026-09-27 KC@MIA KC 30.5", 2), (2, "l", 0)]
    assert conn.execute("SELECT pair_id FROM bets").fetchone()[0] == 1
    assert [tuple(r) for r in conn.execute("SELECT id, pair_id, start_ts FROM opportunities ORDER BY id")] == [(1, 1, "s"), (2, 2, "gone")]
    assert [tuple(r) for r in conn.execute("SELECT id, pair_id FROM trades")] == [(1, 2)]


def test_every_model_writes_only_columns_its_table_has():
    from db.models import Alert, Ledger, Order, Settlement, Trade, Transfer
    conn = database.connect(":memory:")
    for table, model in (("bets", Bet), ("gaps", Gap), ("opportunities", Opportunity), ("trades", Trade), ("settlements", Settlement),
                         ("orders", Order), ("ledger", Ledger), ("alerts", Alert), ("transfers", Transfer)):
        table_columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert set(database.columns(model)) <= set(table_columns), table
        assert set(table_columns) - set(database.columns(model)) <= {"id"}, table      # Nothing in the table the model forgets.


def trade(mode, pair_id=1, **fields):
    from db.models import Trade
    return Trade(mode=mode, pair_id=pair_id, trade="t", signal_ts="2026-09-27T17:30:00+00:00", edge=0.08, quantity=5,
                 yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45,
                 no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.47, pays_at="2026-09-27T20:45:00+00:00", **fields)


def test_paper_and_live_trades_and_settlements_are_kept_apart(tmp_path):
    from db.models import Settlement
    conn = database.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO pairs (id, label, kind, game_date, team_a, team_b, venues, contracts, flags, matched_at) "
                 "VALUES (1, 'game_winner 2026-09-27 CAR@ATL CAR', 'game_winner', '2026-09-27', 'CAR', 'ATL', '', 2, '[]', 'm')")
    held = dict(status="filled", yes_held=5, no_held=5, yes_cost=2.25, no_cost=2.35)
    paper, live = trade("paper", **held), trade("live", **held)
    database.insert_trade(conn, paper)
    assert not database.has_open_trades(conn, "live")
    database.insert_trade(conn, live)
    assert [t.id for t in database.load_open_trades(conn, "paper")] == [paper.id]
    assert [(t.id, t.mode) for t in database.load_open_trades(conn, "live")] == [(live.id, "live")]
    assert database.has_open_trades(conn, "paper") and database.has_open_trades(conn, "live")
    assert database.load_open_game_costs(conn, "live") == [(("2026-09-27", "CAR", "ATL"), [("polymarket_us", 2.25), ("kalshi", 2.35)])]
    database.insert_settlement(conn, Settlement(live.id, "2026-09-27T20:30:00+00:00", mode="live"))
    assert database.load_open_trades(conn, "live") == [] and not database.has_open_trades(conn, "live")
    assert [t.id for t in database.load_open_trades(conn, "paper")] == [paper.id]             # Settling the live trade leaves paper alone.
    assert [s.trade_id for s in database.load_settlements(conn, "live")] == [live.id] and database.load_settlements(conn, "paper") == []


def test_orders_are_stored_before_they_are_sent_and_updated_with_the_answer(tmp_path):
    from db.models import Order
    conn = database.connect(tmp_path / "t.sqlite")
    order = Order(trade_id=7, venue="kalshi", contract_id="k", purpose="open", action="buy", outcome="no", quantity=5,
                  limit_price=0.47, client_id="sa-7-1", sent_at="2026-09-27T17:30:00.010+00:00")
    database.insert_order(conn, order)
    assert order.id == 1 and database.load_orders(conn)[0].status == "sent"
    order.status, order.venue_order_id, order.filled, order.dollars, order.fees = "partial", "abc", 3, 1.45, 0.04
    database.update_order(conn, order)
    assert database.load_orders(conn, trade_id=7) == [order] and database.load_orders(conn, trade_id=8) == []


def test_trades_and_settlements_from_before_live_trading_are_paper(tmp_path):
    import sqlite3
    path = tmp_path / "t.sqlite"
    conn = database.connect(path)
    conn.execute("ALTER TABLE trades DROP COLUMN mode")
    conn.execute("ALTER TABLE settlements DROP COLUMN mode")
    conn.execute("INSERT INTO trades (pair_id, trade, signal_ts, edge, quantity, yes_venue, yes_contract, yes_polarity, yes_limit, yes_filled, yes_cost, "
                 "yes_latency_ms, no_venue, no_contract, no_polarity, no_limit, no_filled, no_cost, no_latency_ms, yes_held, no_held, matched, profit, "
                 "hedge, hedge_pnl, status, pays_at) VALUES (1, 't', 's', 0.05, 5, 'polymarket_us', 'pm', 'yes', 0.45, 5, 2.25, 50, "
                 "'kalshi', 'k', 'yes', 0.47, 5, 2.35, 50, 5, 5, 5, 0.4, 'none', 0, 'filled', 'p')")
    conn.execute("INSERT INTO settlements (trade_id, settled_at) VALUES (1, 's')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit(); conn.close()
    conn = database.connect(path)
    assert (conn.execute("SELECT mode FROM trades").fetchone()[0], conn.execute("SELECT mode FROM settlements").fetchone()[0]) == ("paper", "paper")
