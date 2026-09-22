"""
Round trip every table through the database module.
"""

from db import database
from db.models import StreamGap, Bet, Pair, Contract, Opportunity, Quote


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
    assert list(pairs) == ["champion 2027 BUF"]
    assert pairs["champion 2027 BUF"]["venues"] == "kalshi,polymarket_us"
    assert sorted(m["contract_id"] for m in pairs["champion 2027 BUF"]["members"]) == ["k", "pm"]
    assert pairs["champion 2027 BUF"]["members"][0]["close_time"] == "2027-01-01T00:00:00+00:00"

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
    database.insert_gap(conn, StreamGap("polymarket_us", "2026-09-20T20:39:07+00:00", "2026-09-20T20:39:12+00:00"))
    database.insert_gap(conn, StreamGap("polymarket_us", "2026-09-20T20:37:31+00:00", None))
    assert [g.start_ts[11:19] for g in database.load_gaps(conn, "polymarket_us")] == ["20:37:31", "20:39:07"]
    assert [g.end_ts for g in database.load_gaps(conn, "polymarket_us", "2026-09-20T20:38:00+00:00")] == ["2026-09-20T20:39:12+00:00"]
    assert database.load_gaps(conn, "kalshi") == []


def test_opportunities_append_and_an_old_source_column_is_dropped(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    o = Opportunity("label", "spread", "yes: K buy, no: PMUS buy", "kalshi", "k", "polymarket_us", "pm", "t0", "t1", 60, "t0", 0.02, 100, 2.0, 0, 10.0, 2.04, 74.5)
    database.insert_opportunities(conn, [o])
    database.insert_opportunities(conn, [o])
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2
    conn.execute("ALTER TABLE opportunities ADD COLUMN source TEXT")      # As the retired replay scanner left it.
    conn.commit()
    conn = database.connect(tmp_path / "t.sqlite")
    assert "source" not in [r[1] for r in conn.execute("PRAGMA table_info(opportunities)")]
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2


def test_replace_pairs_clears_labels_of_pairs_that_disappeared(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.upsert_contracts(conn, [contract("polymarket_us", "pm"), contract("kalshi", "k")], "2026-01-01T00:00:00+00:00")
    bets = [Bet(v, cid, "champion", 2027, None, None, None, "BUF", None, "yes") for v, cid in (("polymarket_us", "pm"), ("kalshi", "k"))]
    database.replace_bets(conn, "nfl", bets)
    g = Pair("champion 2027 BUF", "champion", 2027, None, None, None, "BUF", None, bets, [])
    database.replace_pairs(conn, "nfl", [g], "2026-01-01T00:00:00+00:00")
    database.replace_pairs(conn, "nfl", [], "2026-01-02T00:00:00+00:00")
    assert database.load_pairs(conn, "nfl") == {}
    assert conn.execute("SELECT COUNT(*) FROM bets WHERE pair_label IS NOT NULL").fetchone()[0] == 0


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
    """)
    old.commit(); old.close()
    conn = database.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "pairs" in tables and "bet_groups" not in tables and "fee_history" not in tables
    assert conn.execute("SELECT pair_label FROM bets").fetchone()[0] == "champion 2027 BUF"
