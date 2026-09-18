"""
SQLite storage for SportsArb.

One database file holds everything. Contracts from each venue now,
matched pairs and recorded quotes later. One file is easy to open
in any SQLite browser.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sportsarb.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    venue        TEXT NOT NULL,   -- Either 'polymarket' or 'kalshi'.
    contract_id  TEXT NOT NULL,   -- Polymarket outcome token id, or Kalshi market ticker.
    market_id    TEXT NOT NULL,   -- Polymarket conditionId, or Kalshi market ticker.
    event_id     TEXT NOT NULL,   -- Polymarket event slug, or Kalshi event ticker.
    series_id    TEXT,            -- Kalshi series ticker. Polymarket has none.
    sport        TEXT NOT NULL,   -- Our own sport key, for example 'nfl'.
    event_title  TEXT,
    title        TEXT NOT NULL,   -- The market question or title.
    outcome      TEXT NOT NULL,   -- 'Yes', a team name, 'Over', and so on.
    market_type  TEXT,            -- The venue's own market category, for example 'moneyline'.
    line         REAL,            -- Spread or total line when the venue gives one.
    rules        TEXT,
    start_time   TEXT,            -- Game start in ISO 8601 UTC, when known.
    close_time   TEXT,            -- When trading stops, ISO 8601 UTC.
    fee_info     TEXT,            -- JSON describing the venue's fee model for this contract.
    raw_json     TEXT NOT NULL,   -- The venue's untouched market payload.
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (venue, contract_id)
);

CREATE INDEX IF NOT EXISTS idx_contracts_sport ON contracts (sport, venue);
"""


def connect(db_path=DB_PATH):
    """
    Open the database, creating the file and tables if needed.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_contracts(conn, contracts, fetched_at):
    """
    Insert new contracts or refresh existing ones. first_seen is kept as is.
    """
    rows = []
    for c in contracts:
        rows.append((
            c.venue, c.contract_id, c.market_id, c.event_id, c.series_id, c.sport,
            c.event_title, c.title, c.outcome, c.market_type, c.line, c.rules,
            c.start_time, c.close_time,
            json.dumps(c.fee_info) if c.fee_info is not None else None,
            json.dumps(c.raw), fetched_at, fetched_at,
        ))
    conn.executemany("""
        INSERT INTO contracts (
            venue, contract_id, market_id, event_id, series_id, sport,
            event_title, title, outcome, market_type, line, rules,
            start_time, close_time, fee_info, raw_json, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (venue, contract_id) DO UPDATE SET
            market_id = excluded.market_id,
            event_id = excluded.event_id,
            series_id = excluded.series_id,
            sport = excluded.sport,
            event_title = excluded.event_title,
            title = excluded.title,
            outcome = excluded.outcome,
            market_type = excluded.market_type,
            line = excluded.line,
            rules = excluded.rules,
            start_time = excluded.start_time,
            close_time = excluded.close_time,
            fee_info = excluded.fee_info,
            raw_json = excluded.raw_json,
            last_seen = excluded.last_seen
    """, rows)
    conn.commit()


def load_contracts(conn, sport=None, venue=None):
    """
    Return contract rows as dicts, filtered by sport and venue when given.
    """
    sql = "SELECT * FROM contracts WHERE 1=1"
    params = []
    if sport:
        sql += " AND sport = ?"
        params.append(sport)
    if venue:
        sql += " AND venue = ?"
        params.append(venue)
    return [dict(r) for r in conn.execute(sql, params)]
