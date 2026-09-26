"""
SQLite storage for SportsArb.

One database file holds every table, which makes it easy to open in any
SQLite browser. The tables follow the pipeline in order.

    contracts      what each venue lists, written by fetch.py
    bets           each contract restated in venue neutral terms, by classify.py
    pairs          the contracts on both venues for one bet, by match.py
    quotes         order book snapshots for paired contracts, by record.py
    gaps           stretches when a venue's feed was down, also by record.py
    opportunities  every episode the live scanner saw, by scan.py
    trades         every paper trade the executor made, by execute.py, and what each leg paid out, by settle.py
    ledger         every paper cash movement per venue, by balances.py
    transfers      paper rebalancing transfers between venues, by rebalance.py
"""

import sqlite3
from common import jsonutil
from common.paths import DATA_DIR
from db.models import Gap, Ledger, Quote, Trade, Transfer
from pathlib import Path

DB_PATH = DATA_DIR / "sportsarb.sqlite"

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
    pair_id      INTEGER,         -- The pair this bet belongs to, set by match.py. Null while only one venue lists the bet.
    PRIMARY KEY (venue, contract_id)
);

-- A pair keeps its id across matches, found by its label, and its row stays once no bet points at it any more,
-- so the opportunities and trades that refer to it always resolve. A pair is current when a bet points at it.
CREATE TABLE IF NOT EXISTS pairs (
    id           INTEGER PRIMARY KEY,
    label        TEXT NOT NULL UNIQUE,   -- The bet's identity in words, for example 'spread 2026-09-20 CAR@ATL ATL 4.5'.
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

CREATE TABLE IF NOT EXISTS gaps (
    venue        TEXT NOT NULL,
    start_ts     TEXT NOT NULL,   -- When the connection was lost, ISO 8601 UTC.
    end_ts       TEXT,            -- When a new connection was subscribed. Null if the recorder stopped first.
    PRIMARY KEY (venue, start_ts)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id             INTEGER PRIMARY KEY,
    pair_id        INTEGER NOT NULL,   -- The pair, see pairs.
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

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY,
    pair_id        INTEGER NOT NULL,   -- The pair, see pairs.
    trade          TEXT NOT NULL,   -- The two legs in words.
    signal_ts      TEXT NOT NULL,
    edge           REAL NOT NULL,   -- Net dollars per contract at the signal.
    quantity       INTEGER NOT NULL,   -- Contracts wanted on each leg.
    cap            INTEGER,            -- The allocator's cap on contracts per trade when this one was sent.
    yes_venue      TEXT NOT NULL,
    yes_contract   TEXT NOT NULL,
    yes_polarity   TEXT NOT NULL,   -- The side the contract pays on, so settlement knows whether the leg won.
    yes_limit      REAL NOT NULL,
    yes_filled     INTEGER NOT NULL,
    yes_cost       REAL NOT NULL,   -- Dollars paid including fees.
    yes_latency_ms INTEGER NOT NULL,
    yes_fill_ts    TEXT,
    no_venue       TEXT NOT NULL,
    no_contract    TEXT NOT NULL,
    no_polarity    TEXT NOT NULL,
    no_limit       REAL NOT NULL,
    no_filled      INTEGER NOT NULL,
    no_cost        REAL NOT NULL,
    no_latency_ms  INTEGER NOT NULL,
    no_fill_ts     TEXT,
    yes_held       INTEGER NOT NULL,   -- Contracts still held on the yes leg after any flattening.
    no_held        INTEGER NOT NULL,
    matched        INTEGER NOT NULL,   -- Contracts held on both sides after any flattening.
    profit         REAL NOT NULL,   -- Dollars locked in on the matched contracts, after fees.
    hedge          TEXT NOT NULL,   -- How a mismatch was flattened, in words.
    hedge_pnl      REAL NOT NULL,   -- Dollars gained or lost by flattening, after fees.
    status         TEXT NOT NULL,   -- 'sent' while in flight, then 'filled', 'partial', or 'failed'.
    pays_at        TEXT NOT NULL,
    yes_result     TEXT,            -- How the yes leg's contract resolved, 'yes' or 'no', once known.
    yes_payout     REAL,            -- Dollars received on the yes leg, one per contract held when its side won.
    yes_settled_at TEXT,            -- The venue's settlement time for the yes leg.
    no_result      TEXT,
    no_payout      REAL,
    no_settled_at  TEXT,
    settled_at     TEXT             -- When both legs had resolved and the payouts were booked.
);

CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    venue        TEXT NOT NULL,
    amount       REAL NOT NULL,     -- Dollars in or out of the venue balance, positive when money arrives.
    reason       TEXT NOT NULL,     -- 'buy', 'sell', 'payout', 'transfer_out', or 'transfer_in'.
    trade_id     INTEGER,           -- The trade behind a buy, sell, or payout.
    balance      REAL NOT NULL      -- The venue's balance after this entry, so the newest entry gives the balance.
);

CREATE TABLE IF NOT EXISTS transfers (
    id           INTEGER PRIMARY KEY,
    from_venue   TEXT NOT NULL,
    to_venue     TEXT NOT NULL,
    amount       REAL NOT NULL,
    requested_at TEXT NOT NULL,
    expected_at  TEXT NOT NULL,     -- When the money should land, business days after the request.
    arrived_at   TEXT,              -- Set when the money was credited to the receiving venue.
    reason       TEXT NOT NULL      -- 'drift' for the weekly check, 'floor' for a venue running low.
);
"""


# CONNECTION

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
        from live.balances import BALANCE
        conn.execute("ALTER TABLE ledger ADD COLUMN balance REAL NOT NULL DEFAULT 0")
        running = {}
        for row_id, venue, amount in conn.execute("SELECT id, venue, amount FROM ledger ORDER BY id").fetchall():
            running[venue] = running.get(venue, BALANCE) + amount
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
    conn.executescript(SCHEMA)


def migrate_pair_ids(conn):
    """
    Pairs used to be keyed by their label, copied onto bets, opportunities,
    and trades. Give them an id and point the other tables at it. Episodes
    and trades of pairs that had already left the catalog get a bare pair
    row, so their id resolves.
    """
    if "id" not in [r[1] for r in conn.execute("PRAGMA table_info(pairs)")]:
        conn.execute("ALTER TABLE pairs RENAME TO pairs_old")
        conn.executescript(SCHEMA)
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
        conn.executescript(SCHEMA)
        shared = [c for c, in conn.execute(f"SELECT name FROM pragma_table_info('{table}')") if c in old_columns]
        conn.execute(f"""INSERT INTO {table} ({', '.join(shared)}, pair_id)
                         SELECT {', '.join('o.' + c for c in shared)}, p.id FROM {table}_old o JOIN pairs p ON p.label = o.label""")
        conn.execute(f"DROP TABLE {table}_old")


# CONTRACTS

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


def load_fee_infos(conn, sport):
    """
    Return {(venue, contract_id): fee_info} with the fee schedule currently stored for every contract of a sport.
    """
    return {(venue, cid): jsonutil.parse(fee_info, {})
            for venue, cid, fee_info in conn.execute("SELECT venue, contract_id, fee_info FROM contracts WHERE sport = ?", (sport,))}


def event_ids(conn, venue, contract_ids):
    """
    Return {contract_id: event_id} for the contracts of one venue.
    """
    return {cid: event for cid, event in conn.execute(
        f"SELECT contract_id, event_id FROM contracts WHERE venue = ? AND contract_id IN ({','.join('?' * len(contract_ids))})",
        (venue, *contract_ids))} if contract_ids else {}


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
            JOIN pairs p ON p.id = b.pair_id
            WHERE c.venue = ? AND c.sport = ?
              AND (c.close_time IS NULL OR c.close_time > ? OR (c.start_time IS NOT NULL AND c.start_time > ?))
              AND (b.game_date IS NULL OR b.game_date <= ?)
        """, (venue, sport, now, game_started_after, horizon[:10]))
        targets[venue] = [r[0] for r in rows]
    return targets


# BETS

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
                          team_a, team_b, subject, line, polarity, pair_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, [(b.venue, b.contract_id, b.kind, b.season, b.game_date,
           b.team_a, b.team_b, b.subject, b.line, b.polarity, b.pair_id) for b in bets])
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


# PAIRS

def replace_pairs(conn, sport, pairs, matched_at):
    """
    Store the sport's pairs and point each member's bet row at its pair.
    A pair that was stored before keeps its id, found by its label, and
    each Pair and member Bet gets its id set. Pairs that no bet points at
    any more stay in the table, they are just no longer current.
    """
    conn.execute("""
        UPDATE bets SET pair_id = NULL WHERE (venue, contract_id) IN
            (SELECT venue, contract_id FROM contracts WHERE sport = ?)
    """, (sport,))
    for p in pairs:
        conn.execute("""
            INSERT INTO pairs (label, kind, season, game_date, team_a, team_b, subject, line, venues, contracts, flags, matched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (label) DO UPDATE SET kind = excluded.kind, season = excluded.season, game_date = excluded.game_date,
                team_a = excluded.team_a, team_b = excluded.team_b, subject = excluded.subject, line = excluded.line,
                venues = excluded.venues, contracts = excluded.contracts, flags = excluded.flags, matched_at = excluded.matched_at
        """, (p.label, p.kind, p.season, p.game_date, p.team_a, p.team_b, p.subject, p.line,
              ",".join(p.venues), len(p.members), jsonutil.dump(p.flags), matched_at))
        p.id = conn.execute("SELECT id FROM pairs WHERE label = ?", (p.label,)).fetchone()[0]
        for m in p.members:
            m.pair_id = p.id
    conn.executemany("UPDATE bets SET pair_id = ? WHERE venue = ? AND contract_id = ?",
                     [(p.id, m.venue, m.contract_id) for p in pairs for m in p.members])
    conn.commit()


def load_pairs(conn, sport):
    """
    Return {id: pair row dict with a 'members' list of bet row dicts} for
    the current pairs of one sport, the ones with bets pointing at them.
    """
    members = {}
    for r in conn.execute("""
        SELECT b.*, c.event_id, c.start_time, c.close_time FROM bets b JOIN contracts c USING (venue, contract_id)
        WHERE c.sport = ? AND b.pair_id IS NOT NULL""", (sport,)):
        members.setdefault(r["pair_id"], []).append(dict(r))
    return {r["id"]: dict(r, members=members[r["id"]]) for r in conn.execute("SELECT * FROM pairs") if r["id"] in members}


# QUOTES

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


# GAPS

def insert_gap(conn, gap):
    """
    Record a stretch when a venue's feed was down.
    """
    conn.execute("INSERT OR REPLACE INTO gaps (venue, start_ts, end_ts) VALUES (?,?,?)", (gap.venue, gap.start_ts, gap.end_ts))
    conn.commit()


def load_gaps(conn, venue, since=None):
    """
    Return a venue's Gaps in time order, optionally only those starting at or after since.
    """
    sql = "SELECT venue, start_ts, end_ts FROM gaps WHERE venue = ?"
    params = [venue]
    if since:
        sql += " AND start_ts >= ?"
        params.append(since)
    return [Gap(*row) for row in conn.execute(sql + " ORDER BY start_ts", params)]


# OPPORTUNITIES

def insert_opportunities(conn, opportunities):
    """
    Append Opportunities.
    """
    conn.executemany("""
        INSERT INTO opportunities (pair_id, trade, yes_venue, yes_contract, no_venue, no_contract,
                                   start_ts, end_ts, seconds, peak_ts, peak_edge, peak_size, peak_profit,
                                   live, days_held, return_pct, annual_pct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(o.pair_id, o.trade, o.yes_venue, o.yes_contract, o.no_venue, o.no_contract,
           o.start_ts, o.end_ts, o.seconds, o.peak_ts, o.peak_edge, o.peak_size, o.peak_profit,
           o.live, o.days_held, o.return_pct, o.annual_pct) for o in opportunities])
    conn.commit()


# TRADES

def insert_trade(conn, t):
    """
    Append a finished paper Trade and return its id.
    """
    cur = conn.execute("""
        INSERT INTO trades (pair_id, trade, signal_ts, edge, quantity, cap,
                            yes_venue, yes_contract, yes_polarity, yes_limit, yes_filled, yes_cost, yes_latency_ms, yes_fill_ts,
                            no_venue, no_contract, no_polarity, no_limit, no_filled, no_cost, no_latency_ms, no_fill_ts,
                            yes_held, no_held, matched, profit, hedge, hedge_pnl, status, pays_at, settled_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (t.pair_id, t.trade, t.signal_ts, t.edge, t.quantity, t.cap,
          t.yes_venue, t.yes_contract, t.yes_polarity, t.yes_limit, t.yes_filled, t.yes_cost, t.yes_latency_ms, t.yes_fill_ts,
          t.no_venue, t.no_contract, t.no_polarity, t.no_limit, t.no_filled, t.no_cost, t.no_latency_ms, t.no_fill_ts,
          t.yes_held, t.no_held, t.matched, t.profit, t.hedge, t.hedge_pnl, t.status, t.pays_at, t.settled_at))
    conn.commit()
    t.id = cur.lastrowid
    return t.id


def update_trade(conn, t):
    """
    Write a Trade's fills, holdings, and outcome once it is done.
    """
    conn.execute("""
        UPDATE trades SET yes_filled = ?, yes_cost = ?, yes_latency_ms = ?, yes_fill_ts = ?,
                          no_filled = ?, no_cost = ?, no_latency_ms = ?, no_fill_ts = ?,
                          yes_held = ?, no_held = ?, matched = ?, profit = ?, hedge = ?, hedge_pnl = ?, status = ?
        WHERE id = ?
    """, (t.yes_filled, t.yes_cost, t.yes_latency_ms, t.yes_fill_ts, t.no_filled, t.no_cost, t.no_latency_ms, t.no_fill_ts,
          t.yes_held, t.no_held, t.matched, t.profit, t.hedge, t.hedge_pnl, t.status, t.id))
    conn.commit()


def load_open_trades(conn):
    """
    Trades that are done, still hold contracts, and have not settled, with
    their pair's label for log lines and the kickoff of their game, if any.
    """
    return [Trade(**dict(r)) for r in conn.execute("""
        SELECT t.*, p.label,
               (SELECT MAX(c.start_time) FROM contracts c
                WHERE (c.venue = t.yes_venue AND c.contract_id = t.yes_contract) OR (c.venue = t.no_venue AND c.contract_id = t.no_contract)) AS starts_at
        FROM trades t JOIN pairs p ON p.id = t.pair_id
        WHERE t.status != 'sent' AND t.settled_at IS NULL AND t.yes_held + t.no_held > 0 ORDER BY t.id""")]


def settle_trade(conn, t):
    """
    Write what each leg of a Trade paid out and mark it settled.
    """
    conn.execute("""
        UPDATE trades SET yes_result = ?, yes_payout = ?, yes_settled_at = ?, no_result = ?, no_payout = ?, no_settled_at = ?, settled_at = ?
        WHERE id = ?
    """, (t.yes_result, t.yes_payout, t.yes_settled_at, t.no_result, t.no_payout, t.no_settled_at, t.settled_at, t.id))
    conn.commit()


# LEDGER

def add_ledger(conn, entry):
    """
    Record one Ledger entry, with the balance it left behind.
    """
    conn.execute("INSERT INTO ledger (ts, venue, amount, reason, trade_id, balance) VALUES (?,?,?,?,?,?)",
                 (entry.ts, entry.venue, entry.amount, entry.reason, entry.trade_id, entry.balance))
    conn.commit()


def last_balances(conn):
    """
    Return {venue: balance} from each venue's newest ledger entry.
    """
    return {venue: balance for venue, balance in conn.execute(
        "SELECT venue, balance FROM ledger WHERE id IN (SELECT MAX(id) FROM ledger GROUP BY venue)")}


# TRANSFERS

def insert_transfer(conn, transfer):
    """
    Store a requested Transfer and set its id.
    """
    cur = conn.execute("INSERT INTO transfers (from_venue, to_venue, amount, requested_at, expected_at, reason, arrived_at) VALUES (?,?,?,?,?,?,?)",
                       (transfer.from_venue, transfer.to_venue, transfer.amount, transfer.requested_at, transfer.expected_at,
                        transfer.reason, transfer.arrived_at))
    conn.commit()
    transfer.id = cur.lastrowid
    return transfer.id


def load_transfers(conn, pending_only=False):
    """
    Transfers oldest first, optionally only those not yet arrived.
    """
    sql = "SELECT * FROM transfers" + (" WHERE arrived_at IS NULL" if pending_only else "") + " ORDER BY id"
    return [Transfer(**dict(r)) for r in conn.execute(sql)]


def complete_transfer(conn, transfer_id, arrived_at):
    conn.execute("UPDATE transfers SET arrived_at = ? WHERE id = ?", (arrived_at, transfer_id))
    conn.commit()
