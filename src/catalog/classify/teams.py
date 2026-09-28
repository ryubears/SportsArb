"""
Team codes and player names shared by the venue classifiers.

Each sport has an alias file in aliases/, named by our sport key, which
lists every team with its canonical code and the codes the venues use in
slugs and tickers. A code means a team only within its sport, since the
leagues reuse them: DAL is the Cowboys and the Mavericks. Codes are matched
only against slug and ticker pieces, never inside free text. Players have
no alias file. Both venues print the full name in the market title, so a
name reduced to its letters is the key.
"""

import re
from common import jsonutil
from pathlib import Path

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}     # Dropped from player names, since the venues do not agree on them.

ALIAS_DIR = Path(__file__).resolve().parent / "aliases"


def load_aliases():
    """
    Every sport's alias file, as {sport: {team code: {"names": [...], "codes": [...]}}}.
    """
    return {path.stem: jsonutil.read_file(path) for path in sorted(ALIAS_DIR.glob("*.json"))}


ALIASES = load_aliases()
CODE_TO_TEAM = {sport: {c.upper(): team for team, entry in teams.items() for c in entry["codes"]} for sport, teams in ALIASES.items()}


def team_from_code(piece, sport):
    """
    Canonical code of the sport's team for a slug or ticker piece, or None.
    """
    return CODE_TO_TEAM.get(sport, {}).get((piece or "").upper())


def player_key(name):
    """
    A player's name as a matching key: lower case letters and digits, without
    punctuation or a generational suffix. 'A.J. Brown' and 'AJ Brown' both
    become 'aj brown', and 'Aaron Jones Sr.' becomes 'aaron jones'.
    """
    words = re.sub(r"[^a-z0-9 ]", "", name.lower().replace(".", "")).split()
    return " ".join(w for w in words if w not in SUFFIXES)
