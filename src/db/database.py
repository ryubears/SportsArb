"""
SQLite storage for SportsArb.

One database file holds every table, which makes it easy to open in any
SQLite browser. The tables follow the pipeline in order.

    contracts      what each venue lists, as fetched by fetch.py
    bets           each contract restated in venue neutral terms, by classify.py
    pairs          one Polymarket and one Kalshi contract for the same bet, by match.py
    quotes         order book snapshots for paired contracts, by record.py
    opportunities  stretches where a pair could be traded for a profit, by scan.py
"""

import sqlite3
from db.models import Quote
from pathlib import Path
from util import jsonutil

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sportsarb.sqlite"

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

CREATE TABLE IF NOT EXISTS opportunities (
    polymarket_id  TEXT NOT NULL,
    kalshi_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    label          TEXT NOT NULL,   -- Short human readable name of the bet.
    trade          TEXT NOT NULL,   -- Which two legs to buy.
    start_ts       TEXT NOT NULL,   -- When the net edge first went positive.
    end_ts         TEXT NOT NULL,   -- When it went back to zero, or the last quote seen.
    seconds        REAL NOT NULL,
    peak_ts        TEXT NOT NULL,
    peak_edge      REAL NOT NULL,   -- Net dollars per contract at the top of book, at the peak.
    peak_size      REAL NOT NULL,   -- Contracts fillable at a positive net edge, at the peak.
    peak_profit    REAL NOT NULL,   -- Net dollars from filling peak_size, at the peak.
    live           INTEGER NOT NULL,   -- 1 when the game had started, 0 otherwise.
    days_held      REAL,            -- From the peak until the bet pays out, assuming it is held to resolution.
    return_pct     REAL NOT NULL,   -- Net edge over the capital tied up, as a percent.
    annual_pct     REAL             -- return_pct scaled to a year over days_held, without compounding.
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
            c.start_time, c.close_time, jsonutil.dump(c.fee_info), fetched_at, fetched_at,
        ))
    conn.executemany("""
        INSERT INTO contracts (
            venue, contract_id, market_id, event_id, series_id, sport,
            event_title, title, outcome, market_type, line, rules,
            start_time, close_time, fee_info, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
           p.close_gap_days, jsonutil.dump(p.flags), matched_at) for p in pairs])
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


def insert_quotes(conn, quotes):
    """
    Append Quotes. Bids and asks are stored as JSON text.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO quotes (venue, contract_id, ts, bids, asks) VALUES (?,?,?,?,?)",
        [(q.venue, q.contract_id, q.ts, jsonutil.dump(q.bids), jsonutil.dump(q.asks)) for q in quotes],
    )
    conn.commit()


def load_quotes(conn, venue, contract_ids, since=None):
    """
    Return {contract_id: [Quote, ...]} in time order for the given contracts.
    """
    out = {cid: [] for cid in contract_ids}
    sql = "SELECT contract_id, ts, bids, asks FROM quotes WHERE venue = ?"
    params = [venue]
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    sql += " ORDER BY ts"
    for cid, ts, bids, asks in conn.execute(sql, params):
        if cid in out:
            out[cid].append(Quote(venue, cid, ts, jsonutil.parse(bids), jsonutil.parse(asks)))
    return out


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


def replace_opportunities(conn, opportunities):
    """
    Rebuild the table and insert the new Opportunities. Scans are deterministic, so dropping is safe.
    """
    conn.execute("DROP TABLE IF EXISTS opportunities")
    conn.executescript(SCHEMA)
    conn.executemany("""
        INSERT INTO opportunities (polymarket_id, kalshi_id, kind, label, trade, start_ts, end_ts, seconds,
                                   peak_ts, peak_edge, peak_size, peak_profit, live, days_held, return_pct, annual_pct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(o.polymarket_id, o.kalshi_id, o.kind, o.label, o.trade, o.start_ts, o.end_ts, o.seconds,
           o.peak_ts, o.peak_edge, o.peak_size, o.peak_profit, o.live, o.days_held, o.return_pct, o.annual_pct)
          for o in opportunities])
    conn.commit()
