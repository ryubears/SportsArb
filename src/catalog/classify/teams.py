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
ticker pieces, never inside free text. Players have no alias file. Both
venues print the full name in the market title, so a name reduced to its
letters is the key.
"""

import re
from common import jsonutil
from common.venues import VENUES
from pathlib import Path

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}     # Dropped from player names, since the venues do not agree on them.

ALIAS_DIR = Path(__file__).resolve().parent / "aliases"


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
    punctuation or a generational suffix. 'A.J. Brown' and 'AJ Brown' both
    become 'aj brown', and 'Aaron Jones Sr.' becomes 'aaron jones'.
    """
    words = re.sub(r"[^a-z0-9 ]", "", name.lower().replace(".", "")).split()
    return " ".join(w for w in words if w not in SUFFIXES)
