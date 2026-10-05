"""
Tests for numbered migrations: each step runs once per database, and never on a new one.
"""

import sqlite3
from db import database, migrations


def version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_a_new_database_starts_at_the_last_step_without_running_any(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(migrations, "STEPS", [lambda conn: ran.append(1), lambda conn: ran.append(2)])
    conn = database.connect(tmp_path / "t.sqlite")
    assert (version(conn), ran) == (2, [])


def test_an_older_database_runs_each_missing_step_once(tmp_path, monkeypatch):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, ts TEXT, venue TEXT, amount REAL, reason TEXT, trade_id INTEGER, balance REAL)")
    old.execute("PRAGMA user_version = 1")
    old.commit(); old.close()
    ran = []
    monkeypatch.setattr(migrations, "STEPS", [lambda conn: ran.append(1), lambda conn: ran.append(2), lambda conn: ran.append(3)])
    conn = database.connect(path)
    assert (version(conn), ran) == (3, [2, 3])
    conn.close()
    conn = database.connect(path)
    assert (version(conn), ran) == (3, [2, 3])          # Nothing runs twice.


def test_the_quotes_table_is_dropped_and_its_space_given_back(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("PRAGMA journal_mode=WAL")
    old.execute("CREATE TABLE quotes (venue TEXT, contract_id TEXT, ts TEXT, bids TEXT, asks TEXT, PRIMARY KEY (venue, contract_id, ts))")
    old.executemany("INSERT INTO quotes VALUES ('kalshi', 'k', ?, ?, '[]')", [(f"2026-09-27T17:00:{i:05d}", "[[0.5, 100]]" * 20) for i in range(3000)])
    old.execute("PRAGMA user_version = 4")
    old.commit(); old.close()
    before = path.stat().st_size
    conn = database.connect(path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'quotes'").fetchone() is None
    assert version(conn) == len(migrations.STEPS) and path.stat().st_size < before / 4


def test_pairs_from_before_a_second_sport_become_nfl_ones_with_the_sport_in_their_labels(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE pairs (id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, season INTEGER,
                   game_date TEXT, team_a TEXT, team_b TEXT, subject TEXT, line REAL, venues TEXT NOT NULL, contracts INTEGER NOT NULL,
                   flags TEXT NOT NULL, matched_at TEXT NOT NULL)""")
    old.execute("INSERT INTO pairs VALUES (7, 'champion 2027 DEN', 'champion', 2027, NULL, NULL, NULL, 'DEN', NULL, 'kalshi', 2, '[]', 'm')")
    old.execute("PRAGMA user_version = 5")
    old.commit(); old.close()
    conn = database.connect(path)
    assert [tuple(r) for r in conn.execute("SELECT id, sport, label FROM pairs")] == [(7, "nfl", "nfl champion 2027 DEN")]    # Same id.
    assert version(conn) == len(migrations.STEPS)


def test_older_episodes_get_the_minimum_edge_stretch_columns_empty(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE opportunities (id INTEGER PRIMARY KEY, pair_id INTEGER NOT NULL, trade TEXT NOT NULL,
                   yes_venue TEXT NOT NULL, yes_contract TEXT NOT NULL, no_venue TEXT NOT NULL, no_contract TEXT NOT NULL,
                   start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, seconds REAL NOT NULL, peak_ts TEXT NOT NULL, peak_edge REAL NOT NULL,
                   peak_size REAL NOT NULL, peak_profit REAL NOT NULL, live INTEGER NOT NULL, days_held REAL, return_pct REAL NOT NULL,
                   annual_pct REAL)""")
    old.execute("INSERT INTO opportunities VALUES (1, 7, 't', 'kalshi', 'k', 'polymarket_us', 'p', 's', 'e', 1, 'p', 0.1, 10, 1, 0, 1, 11, 400)")
    old.execute("PRAGMA user_version = 6")
    old.commit(); old.close()
    conn = database.connect(path)
    assert tuple(conn.execute("SELECT id, min_edge_seconds, min_edge_size, min_edge_profit FROM opportunities").fetchone()) == (1, None, None, None)
    assert version(conn) == len(migrations.STEPS)


def test_the_transfers_table_is_dropped(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE transfers (id INTEGER PRIMARY KEY, from_venue TEXT, to_venue TEXT, amount REAL)")
    old.execute("PRAGMA user_version = 7")
    old.commit(); old.close()
    conn = database.connect(path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'transfers'").fetchone() is None
    assert version(conn) == len(migrations.STEPS)


def test_trades_lose_their_cap_column_and_keep_their_rows(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, mode TEXT, signal_ts TEXT, quantity INTEGER NOT NULL, cap INTEGER)")
    old.execute("INSERT INTO trades VALUES (1, 'live', '2026-09-29T20:00:00+00:00', 5, 5)")
    old.execute("PRAGMA user_version = 8")
    old.commit(); old.close()
    conn = database.connect(path)
    assert "cap" not in [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    assert tuple(conn.execute("SELECT id, mode, quantity FROM trades").fetchone()) == (1, "live", 5)
    assert version(conn) == len(migrations.STEPS)


def test_the_database_layer_does_not_import_the_live_code():
    import ast, pathlib
    for path in pathlib.Path(database.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in modules if m.split(".")[0] in ("engine", "catalog", "api")], path.name


def test_the_in_play_tests_trades_with_a_leg_on_each_venue_sent_both_orders_at_once_before_it_said(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, mode TEXT, signal_ts TEXT, quantity INTEGER, yes_venue TEXT, no_venue TEXT)")
    # The twins table as it first was, its last column's comment and all, which SQLite keeps in the table's definition.
    old.execute("""CREATE TABLE twins (
    live_trade_id   INTEGER PRIMARY KEY,    -- The live trade, see trades.
    paper_trade_id  INTEGER                 -- Its paper twin, or null when the paper money could not pay for one.
)""")
    old.executemany("INSERT INTO trades VALUES (?, 'live', 's', 5, ?, ?)", [(1, "polymarket_us", "kalshi"), (2, "kalshi", "kalshi")])
    old.executemany("INSERT INTO twins VALUES (?, NULL)", [(1,), (2,)])
    old.execute("PRAGMA user_version = 9")
    old.commit()
    old.close()
    conn = database.connect(path)
    assert [tuple(r) for r in conn.execute("SELECT live_trade_id, sequence FROM twins ORDER BY live_trade_id")] == [(1, "together"), (2, None)]
    assert version(conn) == len(migrations.STEPS)


def test_step_11_gives_older_orders_null_book_times(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    # The orders table as it was, its last column's comment and all, which SQLite keeps in the table's definition.
    old.execute("""CREATE TABLE orders (
    id             INTEGER PRIMARY KEY,
    trade_id       INTEGER NOT NULL,
    response       TEXT                -- The venue's answer as JSON, for reconciling.
)""")
    old.execute("INSERT INTO orders (id, trade_id, response) VALUES (1, 7, '{}')")
    old.execute("PRAGMA user_version = 10")
    old.commit()
    old.close()
    conn = database.connect(path)
    assert [tuple(r) for r in conn.execute("SELECT id, trade_id, response, book_at, book_ts FROM orders")] == [(1, 7, "{}", None, None)]
    conn.execute("INSERT INTO orders (id, trade_id, book_at, book_ts) VALUES (2, 8, 'a', 't')")
    assert tuple(conn.execute("SELECT book_at, book_ts FROM orders WHERE id = 2").fetchone()) == ("a", "t")
    assert version(conn) == len(migrations.STEPS)


def test_step_12_gives_older_trades_no_in_play_so_none_of_them_counts(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE trades (
    id             INTEGER PRIMARY KEY,
    mode           TEXT NOT NULL DEFAULT 'paper',
    signal_ts      TEXT NOT NULL,
    pays_at        TEXT NOT NULL    -- When the slower leg pays out.
)""")
    old.execute("INSERT INTO trades (id, mode, signal_ts, pays_at) VALUES (1, 'live', 's', 'p')")
    old.execute("PRAGMA user_version = 11")
    old.commit()
    old.close()
    conn = database.connect(path)
    assert [tuple(r) for r in conn.execute("SELECT id, in_play FROM trades")] == [(1, None)]
    assert database.count_in_play_trades(conn, "live") == 0
    assert version(conn) == len(migrations.STEPS)


def test_step_13_gives_older_episodes_no_take_so_the_summary_leaves_them_out(tmp_path):
    path = tmp_path / "t.sqlite"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE opportunities (
    id             INTEGER PRIMARY KEY,
    pair_id        INTEGER NOT NULL,
    start_ts       TEXT NOT NULL,   -- When the net edge first went positive.
    min_edge_profit REAL            -- Net dollars from filling them, at the stretch's thinnest moment.
)""")
    old.execute("INSERT INTO opportunities (id, pair_id, start_ts, min_edge_profit) VALUES (1, 7, 's', 0.5)")
    old.execute("PRAGMA user_version = 12")
    old.commit()
    old.close()
    conn = database.connect(path)
    assert [tuple(r) for r in conn.execute("SELECT id, min_edge_profit, take_size, take_profit FROM opportunities")] == [(1, 0.5, None, None)]
    assert version(conn) == len(migrations.STEPS)
