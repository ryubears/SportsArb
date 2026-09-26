"""
The current schema, read from schema.sql.
"""

from pathlib import Path

SQL = Path(__file__).with_name("schema.sql").read_text()


def create(conn):
    """
    Create every table and index the database does not have yet.
    """
    conn.executescript(SQL)
