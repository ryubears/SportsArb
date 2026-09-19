"""
JSON helpers shared across the project.

The venues hand us JSON in odd places, such as a list stored inside a
string field, and the database stores a few columns as JSON text. These
functions cover the missing value cases so callers do not repeat them.
"""

import json
from pathlib import Path


def parse(text, default=None):
    """
    Parse JSON text, or return the default when the text is missing or empty.
    """
    return json.loads(text) if text else default


def dump(value):
    """
    JSON text for a value, or None when the value is None.
    """
    return json.dumps(value) if value is not None else None


def read_file(path):
    """
    Parse a JSON file.
    """
    return json.loads(Path(path).read_text())
