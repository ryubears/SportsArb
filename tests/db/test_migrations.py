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


def test_the_database_layer_does_not_import_the_live_code():
    import ast, pathlib
    for path in pathlib.Path(database.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in modules if m.split(".")[0] in ("live", "catalog", "api")], path.name
