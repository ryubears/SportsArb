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

CREATE TABLE IF NOT EXISTS bets (
    venue        TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- 'champion', 'game_winner', 'spread', 'total', and so on.
    season       INTEGER,         -- The year the season ends.
    game_date    TEXT,            -- YYYY-MM-DD in US Eastern time, for game kinds only.
    team_a       TEXT,            -- Away team code for game kinds.
    team_b       TEXT,            -- Home team code for game kinds.
    subject      TEXT,            -- The team the contract is about, when there is one.
    line         REAL,            -- Spread margin, total points, or wins threshold.
    polarity     TEXT NOT NULL,   -- 'yes' or 'no', see models.Bet.
    PRIMARY KEY (venue, contract_id)
);

CREATE TABLE IF NOT EXISTS pairs (
    polymarket_id        TEXT NOT NULL,
    kalshi_id            TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    season               INTEGER,
    game_date            TEXT,
    team_a               TEXT,
    team_b               TEXT,
    subject              TEXT,
    line                 REAL,
    polymarket_polarity  TEXT NOT NULL,
    kalshi_polarity      TEXT NOT NULL,
    close_gap_days       REAL,            -- Kalshi close time minus Polymarket close time.
    flags                TEXT NOT NULL,   -- JSON list of things to check before trusting the pair.
    matched_at           TEXT NOT NULL,
    PRIMARY KEY (polymarket_id, kalshi_id)
);

CREATE TABLE IF NOT EXISTS quotes (
    venue        TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    ts           TEXT NOT NULL,   -- Our clock, ISO 8601 UTC, when the book changed.
    bids         TEXT NOT NULL,   -- JSON list of [price, size] for the Yes side, best first.
    asks         TEXT NOT NULL,   -- JSON list of [price, size] for the Yes side, best first.
    PRIMARY KEY (venue, contract_id, ts)
);
"""


def connect(db_path=DB_PATH):
    """
    Open the database, creating the file and tables if needed.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Write ahead logging lets readers query while the recorder writes.
    conn.execute("PRAGMA journal_mode=WAL")
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


def replace_bets(conn, sport, bets):
    """
    Drop every bet belonging to the sport's contracts, then insert the new ones.
    """
    conn.execute("""
        DELETE FROM bets WHERE (venue, contract_id) IN
            (SELECT venue, contract_id FROM contracts WHERE sport = ?)
    """, (sport,))
    conn.executemany("""
        INSERT INTO bets (venue, contract_id, kind, season, game_date,
                                 team_a, team_b, subject, line, polarity)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, [(b.venue, b.contract_id, b.kind, b.season, b.game_date,
           b.team_a, b.team_b, b.subject, b.line, b.polarity) for b in bets])
    conn.commit()


def load_bets(conn, sport, venue=None):
    """
    Return bet rows as dicts for one sport, joined with a few contract columns.
    """
    sql = """
        SELECT b.*, c.title, c.outcome, c.event_id, c.series_id, c.close_time, c.start_time
        FROM bets b JOIN contracts c USING (venue, contract_id)
        WHERE c.sport = ?
    """
    params = [sport]
    if venue:
        sql += " AND b.venue = ?"
        params.append(venue)
    return [dict(r) for r in conn.execute(sql, params)]


def replace_pairs(conn, sport, pairs, matched_at):
    """
    Drop every pair belonging to the sport's contracts, then insert the new ones.
    """
    conn.execute("""
        DELETE FROM pairs WHERE polymarket_id IN
            (SELECT contract_id FROM contracts WHERE venue = 'polymarket' AND sport = ?)
    """, (sport,))
    conn.executemany("""
        INSERT INTO pairs (polymarket_id, kalshi_id, kind, season, game_date, team_a, team_b,
                           subject, line, polymarket_polarity, kalshi_polarity,
                           close_gap_days, flags, matched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(p.polymarket_id, p.kalshi_id, p.kind, p.season, p.game_date, p.team_a, p.team_b,
           p.subject, p.line, p.polymarket_polarity, p.kalshi_polarity,
           p.close_gap_days, json.dumps(p.flags), matched_at) for p in pairs])
    conn.commit()


def load_pairs(conn, sport):
    """
    Return pair rows as dicts for one sport.
    """
    sql = """
        SELECT p.* FROM pairs p JOIN contracts c
            ON c.venue = 'polymarket' AND c.contract_id = p.polymarket_id
        WHERE c.sport = ?
    """
    return [dict(r) for r in conn.execute(sql, (sport,))]


def insert_quotes(conn, rows):
    """
    Append quote rows. Each row is (venue, contract_id, ts, bids, asks) with bids and asks as JSON text.
    """
    conn.executemany("INSERT OR REPLACE INTO quotes (venue, contract_id, ts, bids, asks) VALUES (?,?,?,?,?)", rows)
    conn.commit()


def load_recording_targets(conn, sport, now, horizon):
    """
    Return {venue: [contract_id, ...]} for every paired contract that is still
    open and is either a future or a game starting before the horizon.
    """
    targets = {}
    for venue, column in (("polymarket", "polymarket_id"), ("kalshi", "kalshi_id")):
        rows = conn.execute(f"""
            SELECT c.contract_id FROM contracts c
            JOIN bets b ON b.venue = c.venue AND b.contract_id = c.contract_id
            WHERE c.venue = ? AND c.sport = ? AND (c.close_time IS NULL OR c.close_time > ?)
              AND (b.game_date IS NULL OR b.game_date <= ?)
              AND c.contract_id IN (SELECT {column} FROM pairs)
        """, (venue, sport, now, horizon[:10]))
        targets[venue] = [r[0] for r in rows]
    return targets
