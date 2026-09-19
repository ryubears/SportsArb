"""
Time helpers shared across the project.

Every timestamp in the database is an ISO 8601 string in UTC. These
functions convert the venues' formats into that form, and do the small
pieces of arithmetic the other files need on it.

The file is not called time.py because that would shadow Python's own
time module for every script run from the src folder.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def iso(value):
    """
    Turn the venues' assorted timestamp strings into ISO 8601 UTC, or None.
    """
    if not value:
        return None
    s = str(value).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if s.endswith("+00"):
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso():
    """
    Current UTC time as an ISO 8601 string.
    """
    return datetime.now(timezone.utc).isoformat()


def shift(iso_time, hours=0, days=0):
    """
    An ISO timestamp moved forward by the given hours and days.
    """
    return (datetime.fromisoformat(iso_time) + timedelta(hours=hours, days=days)).isoformat()


def seconds_between(a, b):
    """
    Seconds from ISO timestamp a to ISO timestamp b.
    """
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def days_between(a, b):
    """
    Days from ISO timestamp a to ISO timestamp b, as a float.
    """
    return seconds_between(a, b) / 86400


def eastern_date(iso_time):
    """
    Calendar date in US Eastern time for an ISO timestamp, as YYYY-MM-DD.
    """
    return datetime.fromisoformat(iso_time).astimezone(EASTERN).strftime("%Y-%m-%d")
