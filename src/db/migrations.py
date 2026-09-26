"""
Bring older databases up to the current schema, one numbered step at a time.

SQLite's user_version holds the number of the last step a database has
had. Each connection runs the steps after it, in order, and records each
one, so a step runs once per database. A new database is built from
schema.sql, which already has every step in it, so it starts at the last
step and runs none.

To change a table that already exists: change schema.sql, and add a step
at the end of STEPS that makes the same change to an older database.
Steps run after schema.sql, so a table the step needs that is new in the
schema is already there.
"""

from db import schema

LEDGER_START_BALANCE = 10000.0      # What each venue started with when ledgers only held movements.


def migrate(conn, fresh):
    """
    Run the steps this database has not had, recording each. A fresh database only records the last step.
    """
    version = len(STEPS) if fresh else conn.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(STEPS[version:], start=version + 1):
        step(conn)
        conn.execute(f"PRAGMA user_version = {number}")
    if fresh:
        conn.execute(f"PRAGMA user_version = {len(STEPS)}")
    conn.commit()


def step_1_catch_up(conn):
    """
    Every change made before migrations were numbered, each applied only
    when the database still shows the old shape. Derived tables are dropped
    when their columns changed, since a match rebuilds them.
    """
    if "group_label" in [r[1] for r in conn.execute("PRAGMA table_info(bets)")]:
        conn.execute("ALTER TABLE bets RENAME COLUMN group_label TO pair_label")     # Turned into pair_id below.
    conn.execute("DROP TABLE IF EXISTS bet_groups")
    conn.execute("DROP TABLE IF EXISTS fee_history")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stream_gaps'").fetchone():
        conn.execute("INSERT OR REPLACE INTO gaps (venue, start_ts, end_ts) SELECT venue, start_ts, end_ts FROM stream_gaps")
        conn.execute("DROP TABLE stream_gaps")
    ledger_columns = [r[1] for r in conn.execute("PRAGMA table_info(ledger)")]
    if ledger_columns and "balance" not in ledger_columns:
        # Older ledgers only held the movements. Replay them from the starting balance to fill in the running balance.
        conn.execute("ALTER TABLE ledger ADD COLUMN balance REAL NOT NULL DEFAULT 0")
        running = {}
        for row_id, venue, amount in conn.execute("SELECT id, venue, amount FROM ledger ORDER BY id").fetchall():
            running[venue] = running.get(venue, LEDGER_START_BALANCE) + amount
            conn.execute("UPDATE ledger SET balance = ? WHERE id = ?", (running[venue], row_id))
    trade_columns = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    if trade_columns and "cap" not in trade_columns:
        conn.execute("ALTER TABLE trades ADD COLUMN cap INTEGER")
    if trade_columns and "yes_result" not in trade_columns:
        for column, kind in (("yes_result", "TEXT"), ("yes_payout", "REAL"), ("yes_settled_at", "TEXT"),
                             ("no_result", "TEXT"), ("no_payout", "REAL"), ("no_settled_at", "TEXT")):
            conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {kind}")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settlements'").fetchone():
        # Settlements used to be a table of legs. Fold each leg into its trade's columns.
        for trade_id, side, result, payout, settled_at in conn.execute(
                "SELECT trade_id, side, result, payout, settled_at FROM settlements").fetchall():
            conn.execute(f"UPDATE trades SET {side}_result = ?, {side}_payout = ?, {side}_settled_at = ? WHERE id = ?",
                         (result, payout, settled_at, trade_id))
        conn.execute("DROP TABLE settlements")
    columns = [r[1] for r in conn.execute("PRAGMA table_info(opportunities)")]
    if "scope" in columns:
        conn.execute("DROP TABLE opportunities")
    if "source" in columns:
        # Rows from the retired replay scanner stay as part of the log, without the column that told them apart.
        conn.execute("ALTER TABLE opportunities DROP COLUMN source")
    migrate_pair_ids(conn)
    schema.create(conn)


def migrate_pair_ids(conn):
    """
    Pairs used to be keyed by their label, copied onto bets, opportunities,
    and trades. Give them an id and point the other tables at it. Episodes
    and trades of pairs that had already left the catalog get a bare pair
    row, so their id resolves.
    """
    if "id" not in [r[1] for r in conn.execute("PRAGMA table_info(pairs)")]:
        conn.execute("ALTER TABLE pairs RENAME TO pairs_old")
        schema.create(conn)
        conn.execute("""INSERT INTO pairs (label, kind, season, game_date, team_a, team_b, subject, line, venues, contracts, flags, matched_at)
                        SELECT label, kind, season, game_date, team_a, team_b, subject, line, venues, contracts, flags, matched_at FROM pairs_old""")
        conn.execute("DROP TABLE pairs_old")
    if "pair_label" in [r[1] for r in conn.execute("PRAGMA table_info(bets)")]:
        conn.execute("ALTER TABLE bets ADD COLUMN pair_id INTEGER")
        conn.execute("UPDATE bets SET pair_id = (SELECT id FROM pairs WHERE label = bets.pair_label)")
        conn.execute("ALTER TABLE bets DROP COLUMN pair_label")
    for table, first_ts in (("opportunities", "start_ts"), ("trades", "signal_ts")):
        old_columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "label" not in old_columns:
            continue
        conn.execute(f"""INSERT OR IGNORE INTO pairs (label, kind, venues, contracts, flags, matched_at)
                         SELECT label, kind, '', 0, '[]', MIN({first_ts}) FROM {table} GROUP BY label""")
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        schema.create(conn)
        shared = [c for c, in conn.execute(f"SELECT name FROM pragma_table_info('{table}')") if c in old_columns]
        conn.execute(f"""INSERT INTO {table} ({', '.join(shared)}, pair_id)
                         SELECT {', '.join('o.' + c for c in shared)}, p.id FROM {table}_old o JOIN pairs p ON p.label = o.label""")
        conn.execute(f"DROP TABLE {table}_old")


# Step n brings a database from user_version n - 1 to n. Only ever add to the end.
STEPS = [step_1_catch_up]
