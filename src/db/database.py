"""
SQLite storage for SportsArb.

One database file holds every table, which makes it easy to open in any
SQLite browser. The tables follow the pipeline in order.

    contracts      what each venue lists, written by fetch.py
    bets           each contract restated in venue neutral terms, by classify.py
    pairs          the contracts on both venues for one bet, by match.py
    quotes         order book snapshots for paired contracts, by record.py
    stream_gaps    stretches when a venue's feed was down, also by record.py
    opportunities  every episode the live scanner saw, by record.py
"""

import sqlite3
from common import jsonutil
from db.models import Quote, StreamGap
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sportsarb.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    venue        TEXT NOT NULL,   -- 'kalshi' or 'polymarket_us'.
    contract_id  TEXT NOT NULL,   -- Kalshi market ticker, or Polymarket US market slug.
    market_id    TEXT NOT NULL,   -- Kalshi market ticker, or Polymarket US market id.
    event_id     TEXT NOT NULL,   -- Kalshi event ticker, or Polymarket US event slug.
    series_id    TEXT,            -- Kalshi series ticker, or Polymarket US series slug.
    sport        TEXT NOT NULL,   -- Our own sport key, for example 'nfl'.
    event_title  TEXT,
    title        TEXT NOT NULL,   -- The market question or title.
    outcome      TEXT NOT NULL,   -- 'Yes', a team name, 'Over', and so on.
    market_type  TEXT,            -- The venue's own market category, for example 'moneyline'.
    line         REAL,            -- Spread or total line when the venue gives one.
    rules        TEXT,
    start_time   TEXT,            -- Game start in ISO 8601 UTC, when known.
    close_time   TEXT,            -- When trading stops, or settles if sooner, ISO 8601 UTC.
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
    pair_label   TEXT,            -- The pair this bet belongs to, set by match.py.
    PRIMARY KEY (venue, contract_id)
);

CREATE TABLE IF NOT EXISTS pairs (
    label        TEXT PRIMARY KEY,   -- The bet's identity in words, for example 'spread 2026-09-20 CAR@ATL ATL 4.5'.
    kind         TEXT NOT NULL,
    season       INTEGER,
    game_date    TEXT,
    team_a       TEXT,
    team_b       TEXT,
    subject      TEXT,
    line         REAL,
    venues       TEXT NOT NULL,      -- Comma separated venues with a member contract.
    contracts    INTEGER NOT NULL,   -- Member contracts.
    flags        TEXT NOT NULL,      -- JSON list of things to check before trusting the pair.
    matched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    venue        TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    ts           TEXT NOT NULL,   -- Our clock, ISO 8601 UTC, when the book changed.
    bids         TEXT NOT NULL,   -- JSON list of [price, size] for the Yes side, best first.
    asks         TEXT NOT NULL,   -- JSON list of [price, size] for the Yes side, best first.
    PRIMARY KEY (venue, contract_id, ts)
);

CREATE TABLE IF NOT EXISTS stream_gaps (
    venue        TEXT NOT NULL,
    start_ts     TEXT NOT NULL,   -- When the connection was lost, ISO 8601 UTC.
    end_ts       TEXT,            -- When a new connection was subscribed. Null if the recorder stopped first.
    PRIMARY KEY (venue, start_ts)
);

CREATE TABLE IF NOT EXISTS opportunities (
    label          TEXT NOT NULL,   -- The pair's label.
    kind           TEXT NOT NULL,
    trade          TEXT NOT NULL,   -- The two legs in words.
    yes_venue      TEXT NOT NULL,   -- Where the yes exposure was cheapest at the peak.
    yes_contract   TEXT NOT NULL,
    no_venue       TEXT NOT NULL,   -- Where the no exposure was cheapest at the peak.
    no_contract    TEXT NOT NULL,
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


def connect(db_path=None):
    """
    Open the database, creating the file and tables if needed. Uses DB_PATH unless a path is given.
    """
    db_path = Path(db_path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # A long busy timeout lets the recorder and the hourly catalog job share the file.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # Write ahead logging lets readers query while the recorder writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn):
    """
    Bring older databases up to the current schema. Derived tables are
    dropped when their columns changed, since a match rebuilds them.
    """
    bet_columns = [r[1] for r in conn.execute("PRAGMA table_info(bets)")]
    if "group_label" in bet_columns:
        conn.execute("ALTER TABLE bets RENAME COLUMN group_label TO pair_label")
    elif "pair_label" not in bet_columns:
        conn.execute("ALTER TABLE bets ADD COLUMN pair_label TEXT")
    conn.execute("DROP TABLE IF EXISTS bet_groups")
    conn.execute("DROP TABLE IF EXISTS fee_history")
    columns = [r[1] for r in conn.execute("PRAGMA table_info(opportunities)")]
    if "scope" in columns:
        conn.execute("DROP TABLE opportunities")
    if "source" in columns:
        # Rows from the retired replay scanner stay as part of the log, without the column that told them apart.
        conn.execute("ALTER TABLE opportunities DROP COLUMN source")
    conn.executescript(SCHEMA)


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
                          team_a, team_b, subject, line, polarity, pair_label)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, [(b.venue, b.contract_id, b.kind, b.season, b.game_date,
           b.team_a, b.team_b, b.subject, b.line, b.polarity, b.pair_label) for b in bets])
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
    Drop every pair belonging to the sport, then insert the new ones and
    write each member's pair label on its bet row.
    """
    conn.execute("""
        UPDATE bets SET pair_label = NULL WHERE (venue, contract_id) IN
            (SELECT venue, contract_id FROM contracts WHERE sport = ?)
    """, (sport,))
    conn.execute("DELETE FROM pairs WHERE label NOT IN (SELECT pair_label FROM bets WHERE pair_label IS NOT NULL)")
    conn.executemany("""
        INSERT OR REPLACE INTO pairs (label, kind, season, game_date, team_a, team_b, subject, line,
                                      venues, contracts, flags, matched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(p.label, p.kind, p.season, p.game_date, p.team_a, p.team_b, p.subject, p.line,
           ",".join(p.venues), len(p.members), jsonutil.dump(p.flags), matched_at) for p in pairs])
    conn.executemany("UPDATE bets SET pair_label = ? WHERE venue = ? AND contract_id = ?",
                     [(p.label, m.venue, m.contract_id) for p in pairs for m in p.members])
    conn.commit()


def load_pairs(conn, sport):
    """
    Return {label: pair row dict with a 'members' list of bet row dicts} for one sport.
    """
    pairs = {}
    for r in conn.execute("SELECT * FROM pairs"):
        pairs[r["label"]] = dict(r, members=[])
    for r in conn.execute("""
        SELECT b.*, c.start_time, c.close_time FROM bets b JOIN contracts c USING (venue, contract_id)
        WHERE c.sport = ? AND b.pair_label IS NOT NULL""", (sport,)):
        if r["pair_label"] in pairs:
            pairs[r["pair_label"]]["members"].append(dict(r))
    return pairs


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


def insert_gap(conn, gap):
    """
    Record a stretch when a venue's feed was down.
    """
    conn.execute("INSERT OR REPLACE INTO stream_gaps (venue, start_ts, end_ts) VALUES (?,?,?)", (gap.venue, gap.start_ts, gap.end_ts))
    conn.commit()


def load_gaps(conn, venue, since=None):
    """
    Return a venue's StreamGaps in time order, optionally only those starting at or after since.
    """
    sql = "SELECT venue, start_ts, end_ts FROM stream_gaps WHERE venue = ?"
    params = [venue]
    if since:
        sql += " AND start_ts >= ?"
        params.append(since)
    return [StreamGap(*row) for row in conn.execute(sql + " ORDER BY start_ts", params)]


def load_recording_targets(conn, sport, now, horizon, venues, game_started_after):
    """
    Return {venue: [contract_id, ...]} for every contract in a pair that
    is still open and is either a future or a game starting before the
    horizon. A game contract also counts as open
    while its game may still be in play, meaning it started after
    game_started_after, in case a venue's close time is the kickoff even
    though its markets trade through the game.
    """
    targets = {}
    for venue in venues:
        rows = conn.execute("""
            SELECT c.contract_id FROM contracts c
            JOIN bets b ON b.venue = c.venue AND b.contract_id = c.contract_id
            JOIN pairs p ON p.label = b.pair_label
            WHERE c.venue = ? AND c.sport = ?
              AND (c.close_time IS NULL OR c.close_time > ? OR (c.start_time IS NOT NULL AND c.start_time > ?))
              AND (b.game_date IS NULL OR b.game_date <= ?)
        """, (venue, sport, now, game_started_after, horizon[:10]))
        targets[venue] = [r[0] for r in rows]
    return targets


def insert_opportunities(conn, opportunities):
    """
    Append Opportunities.
    """
    conn.executemany("""
        INSERT INTO opportunities (label, kind, trade, yes_venue, yes_contract, no_venue, no_contract,
                                   start_ts, end_ts, seconds, peak_ts, peak_edge, peak_size, peak_profit,
                                   live, days_held, return_pct, annual_pct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(o.label, o.kind, o.trade, o.yes_venue, o.yes_contract, o.no_venue, o.no_contract,
           o.start_ts, o.end_ts, o.seconds, o.peak_ts, o.peak_edge, o.peak_size, o.peak_profit,
           o.live, o.days_held, o.return_pct, o.annual_pct) for o in opportunities])
    conn.commit()


def load_fee_infos(conn, sport):
    """
    Return {(venue, contract_id): fee_info} with the fee schedule currently stored for every contract of a sport.
    """
    return {(venue, cid): jsonutil.parse(fee_info, {})
            for venue, cid, fee_info in conn.execute("SELECT venue, contract_id, fee_info FROM contracts WHERE sport = ?", (sport,))}
