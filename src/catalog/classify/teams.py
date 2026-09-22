"""
Team codes shared by the venue classifiers.

The alias file lists every team with its canonical code and the codes the
venues use in slugs and tickers. Codes are matched only against slug and
ticker pieces, never inside free text.
"""

from common import jsonutil
from pathlib import Path

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
