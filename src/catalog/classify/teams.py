"""
Team codes and player names shared by the venue classifiers.

The alias file lists every team with its canonical code and the codes the
venues use in slugs and tickers. Codes are matched only against slug and
ticker pieces, never inside free text. Players have no alias file. Both
venues print the full name in the market title, so a name reduced to its
letters is the key.
"""

import re
from common import jsonutil
from pathlib import Path

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}     # Dropped from player names, since the venues do not agree on them.

ALIAS_FILE = Path(__file__).resolve().parent / "aliases.json"


def load_aliases():
    """
    The alias file as {team code: {"names": [...], "codes": [...]}}.
    """
    return jsonutil.read_file(ALIAS_FILE)


ALIASES = load_aliases()
CODE_TO_TEAM = {c.upper(): team for team, entry in ALIASES.items() for c in entry["codes"]}


def team_from_code(piece):
    """
    Canonical team code for a slug or ticker piece, or None.
    """
    return CODE_TO_TEAM.get((piece or "").upper())


def player_key(name):
    """
    A player's name as a matching key: lower case letters and digits, without
    punctuation or a generational suffix. 'A.J. Brown' and 'AJ Brown' both
    become 'aj brown', and 'Aaron Jones Sr.' becomes 'aaron jones'.
    """
    words = re.sub(r"[^a-z0-9 ]", "", name.lower().replace(".", "")).split()
    return " ".join(w for w in words if w not in SUFFIXES)
