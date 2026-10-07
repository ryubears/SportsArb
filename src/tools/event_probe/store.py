"""
The event probe's own database, data/event_probe.sqlite, apart from the
bot's, so the probe never writes where the live process does, and
deleting the file removes all it kept. Times are seconds since 1970, by
this machine's clock unless a column says the venue's or the feed's.
"""

import sqlite3
from common.paths import DATA_DIR

PATH = DATA_DIR / "event_probe.sqlite"

SCHEMA = """
-- Each game watched, its teams in the catalog's codes.
CREATE TABLE IF NOT EXISTS games (
    sport TEXT, game_id TEXT, game_date TEXT, away TEXT, home TEXT,
    start REAL,                 -- Scheduled start.
    watched_at REAL,            -- When the probe began reading it.
    PRIMARY KEY (sport, game_id)
);
-- Each contract on a watched game, and the bet it stands for, see markets.Watched.
CREATE TABLE IF NOT EXISTS watched (
    venue TEXT, contract_id TEXT, sport TEXT, game_id TEXT, kind TEXT, subject TEXT, line REAL, polarity TEXT,
    fee_info TEXT,              -- The contract's fee schedule as JSON, so the report prices orders without the bot's database.
    PRIMARY KEY (venue, contract_id)
);
-- Every read of a game's feed.
CREATE TABLE IF NOT EXISTS reads (
    sport TEXT, game_id TEXT,
    ts REAL,                    -- When the read's last byte arrived.
    seconds REAL,               -- How long the read took.
    age REAL,                   -- How old the cache in front said the content was, null when it said nothing.
    made REAL,                  -- When the feed says it made the content, MLB only.
    play_ended REAL,            -- When the newest play ended, by the feed, MLB only.
    changed INTEGER,            -- 1 when the content differs from the last read's.
    state TEXT, away INTEGER, home INTEGER, play TEXT
);
-- Each change a read made to a contract's bet: a play that settled it, or undid that.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, sport TEXT, game_id TEXT, venue TEXT, contract_id TEXT,
    why TEXT,                   -- 'crossed': a count passed the line; 'pulled': a pitcher left short of it; 'final': the game
                                -- ended; 'reversed': a bet settled by an earlier read is open or the other way now.
    winner TEXT,                -- The contract's outcome that pays now, 'yes' or 'no', or null when open again.
    value REAL,                 -- The count the line is set against, null for a winner or a spread.
    ts REAL,                    -- When the read that showed it arrived.
    play_ended REAL, made REAL, play TEXT
);
-- Every change of a watched contract's book, from the Yes side, best first, as JSON [[price, size], ...].
CREATE TABLE IF NOT EXISTS books (
    venue TEXT, contract_id TEXT,
    ts REAL,                    -- When it arrived.
    at REAL,                    -- The venue's time for the change, null when the message gave none.
    bids TEXT, asks TEXT
);
CREATE INDEX IF NOT EXISTS idx_books_contract ON books (venue, contract_id, ts);
-- What a venue said of a watched market's trading, why in a word when it stopped, null when it trades again.
CREATE TABLE IF NOT EXISTS states (venue TEXT, contract_id TEXT, ts REAL, why TEXT);
-- Stretches when a venue's connection was down, so its books were not followed.
CREATE TABLE IF NOT EXISTS gaps (venue TEXT, start_ts TEXT, end_ts TEXT, contracts INTEGER);
"""
REPLACE = {"games", "watched"}      # Tables keyed by what they describe, where a row seen again replaces the old one.


class Store:
    """
    The database, with rows held until flush(), which the probe calls each
    second, so a busy book costs one write a second rather than one each.
    """

    def __init__(self, path=PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")        # The report can read while the probe writes.
        self.conn.executescript(SCHEMA)
        self.pending = {}

    def add(self, table, row):
        self.pending.setdefault(table, []).append(row)

    def flush(self):
        for table, rows in self.pending.items():
            verb = "INSERT OR REPLACE" if table in REPLACE else "INSERT"
            self.conn.executemany(f"{verb} INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)
        self.pending = {}
        self.conn.commit()
