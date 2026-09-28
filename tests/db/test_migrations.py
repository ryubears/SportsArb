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


def test_the_database_layer_does_not_import_the_live_code():
    import ast, pathlib
    for path in pathlib.Path(database.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in modules if m.split(".")[0] in ("engine", "catalog", "api")], path.name
