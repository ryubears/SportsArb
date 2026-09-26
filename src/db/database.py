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
    trades         every paper trade the executor made, by execute.py
    settlements    how each trade's legs paid out, by settle.py
    ledger         every paper cash movement per venue, by balances.py
    transfers      paper rebalancing transfers between venues, by rebalance.py

The tables themselves are in schema.sql, and the steps that bring older
databases up to them in migrations.py. This file holds the reads and writes.
"""

import sqlite3
from dataclasses import asdict, fields
from common import jsonutil
from common.paths import DATA_DIR
from db import migrations, schema
from db.models import Bet, Gap, Ledger, Opportunity, Quote, Settlement, Trade, Transfer
from pathlib import Path

DB_PATH = DATA_DIR / "sportsarb.sqlite"

# Model fields that are read from other tables or set by the database, so they are never written.
NOT_STORED = {"id", "label", "starts_at"}
# What a Trade's update and settlement change, in the order they happen.
TRADE_FILLS = ["yes_filled", "yes_cost", "yes_latency_ms", "yes_fill_ts", "no_filled", "no_cost", "no_latency_ms", "no_fill_ts",
               "yes_held", "no_held", "matched", "profit", "hedge", "hedge_pnl", "status"]


def columns(model):
    """
    The columns a model's rows are written to: its fields, less the ones never stored.
    """
    return [f.name for f in fields(model) if f.name not in NOT_STORED]


def insert_sql(table, model, verb="INSERT"):
    """
    An insert of every stored field of a model, with a named parameter per column.
    """
    names = columns(model)
    return f"{verb} INTO {table} ({', '.join(names)}) VALUES ({', '.join(':' + n for n in names)})"


def update_sql(table, names):
    """
    An update of the named columns of one row, found by its id.
    """
    return f"UPDATE {table} SET {', '.join(f'{n} = :{n}' for n in names)} WHERE id = :id"


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
    fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone() is None
    schema.create(conn)
    migrations.migrate(conn, fresh)
    return conn


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
    conn.executemany(insert_sql("bets", Bet), [asdict(b) for b in bets])
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


def load_kickoffs(conn):
    """
    Return {(game_date, team_a, team_b): kickoff} for every game with a current pair, from its contracts' latest start time.
    """
    return {(d, a, b): kickoff for d, a, b, kickoff in conn.execute("""
        SELECT p.game_date, p.team_a, p.team_b, MAX(c.start_time)
        FROM pairs p JOIN bets b ON b.pair_id = p.id JOIN contracts c ON c.venue = b.venue AND c.contract_id = b.contract_id
        WHERE p.game_date IS NOT NULL GROUP BY 1, 2, 3 HAVING MAX(c.start_time) IS NOT NULL""")}


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
    conn.execute(insert_sql("gaps", Gap, "INSERT OR REPLACE"), asdict(gap))
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
    conn.executemany(insert_sql("opportunities", Opportunity), [asdict(o) for o in opportunities])
    conn.commit()


# TRADES

def insert_trade(conn, t):
    """
    Append a finished paper Trade and return its id.
    """
    cur = conn.execute(insert_sql("trades", Trade), asdict(t))
    conn.commit()
    t.id = cur.lastrowid
    return t.id


def update_trade(conn, t):
    """
    Write a Trade's fills, holdings, and outcome once it is done.
    """
    conn.execute(update_sql("trades", TRADE_FILLS), asdict(t))
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
        WHERE t.status != 'sent' AND t.yes_held + t.no_held > 0
          AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id) ORDER BY t.id""")]


def has_open_trades(conn):
    """
    Whether any trade is still in flight or holds contracts that have not settled.
    """
    return conn.execute("""
        SELECT 1 FROM trades t WHERE (t.status = 'sent' OR t.yes_held + t.no_held > 0)
          AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id) LIMIT 1""").fetchone() is not None


def load_open_game_costs(conn):
    """
    What each unsettled trade on a game still holds, as [((game_date, team_a, team_b), [(venue, dollars), (venue, dollars)])],
    one entry per trade with its yes leg's cost first.
    """
    return [((d, a, b), [(yv, yc), (nv, nc)]) for d, a, b, yv, yc, nv, nc in conn.execute("""
        SELECT p.game_date, p.team_a, p.team_b, t.yes_venue, t.yes_cost, t.no_venue, t.no_cost
        FROM trades t JOIN pairs p ON p.id = t.pair_id
        WHERE t.yes_held + t.no_held > 0 AND p.game_date IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)""")]


def insert_settlement(conn, settlement):
    """
    Store how a trade's legs paid out, which marks the trade settled.
    """
    conn.execute(insert_sql("settlements", Settlement), asdict(settlement))
    conn.commit()


def load_settlements(conn):
    """
    Every Settlement, oldest trade first.
    """
    return [Settlement(**dict(r)) for r in conn.execute("SELECT * FROM settlements ORDER BY trade_id")]


# LEDGER

def add_ledger(conn, entry):
    """
    Record one Ledger entry, with the balance it left behind.
    """
    conn.execute(insert_sql("ledger", Ledger), asdict(entry))
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
    cur = conn.execute(insert_sql("transfers", Transfer), asdict(transfer))
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
