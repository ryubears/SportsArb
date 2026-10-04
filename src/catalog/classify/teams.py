"""
Team codes and player names shared by the venue classifiers.

Each sport has an alias file in aliases/, named by our sport key, which
lists every team with its canonical code, its names for whoever reads the
file, and the codes the venues use in slugs and tickers. A code means a
team only within its sport, since the leagues reuse them: DAL is the
Cowboys and the Mavericks. A team's codes are one list when the venues
share them, as in the NFL, or a list for each venue when they clash, as
in college football, where SDST is South Dakota State on Kalshi and San
Diego State on Polymarket US. Codes are matched only against slug and
ticker pieces, never inside free text. A sport whose futures name only
people, a driver or a fighter, has an empty file. Players have no alias
file. Both venues print the full name in the market title, so a name
reduced to its letters is the key, and the few names the venues spell
apart are in PLAYER_ALIASES.
"""

import re
import unicodedata
from common import jsonutil
from common.venues import VENUES
from pathlib import Path

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}     # Dropped from player names, since the venues do not agree on them.
LETTERS = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"})   # No accent to drop.
# A name one venue spells apart from the other, by its key, to the key of the other's spelling.
PLAYER_ALIASES = {"andrea kimi antonelli": "kimi antonelli", "yeremy pino": "yeremi pino"}
# What a market on a person can name that is not a person, so is never paired by name: Kalshi lists 'Vacant' for a title.
NOT_PEOPLE = {"vacant", "other", "any other", "field", "tie", "none", "no one", "nobody"}

ALIAS_DIR = Path(__file__).resolve().parent / "aliases"

# ELECTIONS, which both venues hold by state.
STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
          "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
          "VT", "VA", "WA", "WV", "WI", "WY"}
# Where the general election can pit two of one party, California's and Washington's top two and Alaska's top four. There
# Kalshi pays a party's market on any member of it taking the seat and Polymarket US on its nominee winning, which can differ.
TOP_TWO_STATES = {"CA", "WA", "AK"}


def race(state, district=None):
    """
    A race's name in a bet: the state, 'GA', or for a House seat the state and district, 'AZ-01' or 'AK-AL' for one at large.
    """
    if not district:
        return state
    return f"{state}-{district.zfill(2) if district.isdigit() else district.upper()}"


def load_aliases():
    """
    Every sport's alias file, as {sport: {team code: {"names": [...], "codes": [...] or {venue: [...]}}}}.
    """
    return {path.stem: jsonutil.read_file(path) for path in sorted(ALIAS_DIR.glob("*.json"))}


def venue_codes(entry, venue):
    """
    The codes a venue uses for a team: the venue's own list, or the one list every venue shares.
    """
    codes = entry["codes"]
    return codes.get(venue, []) if isinstance(codes, dict) else codes


ALIASES = load_aliases()
CODE_TO_TEAM = {sport: {venue: {c.upper(): team for team, entry in teams.items() for c in venue_codes(entry, venue)} for venue in VENUES}
                for sport, teams in ALIASES.items()}


def team_from_code(piece, sport, venue):
    """
    Canonical code of the sport's team that a piece of the venue's slug or ticker names, or None.
    """
    return CODE_TO_TEAM.get(sport, {}).get(venue, {}).get((piece or "").upper())


def player_key(name):
    """
    A player's name as a matching key: lower case letters and digits, without
    accents, punctuation, or a generational suffix, a hyphen read as a space.
    'A.J. Brown' and 'AJ Brown' both become 'aj brown', 'Aaron Jones Sr.'
    becomes 'aaron jones', Kalshi's 'Ronald Acuña Jr.' and Polymarket US's
    'Ronald Acuna' become 'ronald acuna', and 'Kiernan Dewsbury-Hall' and
    'Kiernan Dewsbury Hall' both 'kiernan dewsbury hall'. A name in
    PLAYER_ALIASES becomes the other venue's spelling.
    """
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", name.lower().translate(LETTERS)) if not unicodedata.combining(ch))
    words = re.sub(r"[^a-z0-9 ]", "", plain.replace(".", "").replace("-", " ")).split()
    key = " ".join(w for w in words if w not in SUFFIXES)
    return PLAYER_ALIASES.get(key, key)


def person(name):
    """
    The key of the person a market names, or None when it names no one, see NOT_PEOPLE.
    """
    key = player_key(name or "")
    return key if key and key not in NOT_PEOPLE else None


def side_key(name):
    """
    The key of one side of a match between two people, a player, a fighter, or a driver: the person's key with its words
    in order, since the venues put a name's parts in different orders, Kalshi's 'Wang Cong' being Polymarket US's
    'Cong Wang'. None when the name is no one's.
    """
    key = person(name)
    return " ".join(sorted(key.split())) if key else None


def match_sides(title):
    """
    The keys of the two people a match's title names, 'Valentin Vacherot vs. Arthur Fils', in the title's order, or None
    unless it names two people by their full names, as some of Kalshi's titles do only by the last.
    """
    parts = re.split(r"\s+vs\.?\s+", title or "")
    if len(parts) != 2 or any(len(p.split()) < 2 for p in parts):
        return None
    keys = tuple(side_key(p) for p in parts)
    return keys if all(keys) and keys[0] != keys[1] else None
