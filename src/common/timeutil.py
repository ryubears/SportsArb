"""
Time helpers shared across the project.

Every timestamp in the database is an ISO 8601 string in UTC. These
functions convert the venues' formats into that form, and do the small
pieces of arithmetic the other files need on it.

The file is not called time.py because that would shadow Python's own
time module for every script run from the src folder.
"""

import re
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


def epoch(value):
    """
    Seconds since 1970 for an ISO 8601 timestamp, such as the venues send
    with their messages, or None when there is none or it does not parse.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def at_seconds(seconds):
    """
    Seconds since 1970 as ISO 8601 UTC, as now_iso() gives the time.
    """
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="microseconds")


def utc_minute(seconds):
    """
    Seconds since 1970 as a time to the minute in UTC for people to read, such as '2026-10-21 14:13 UTC'.
    """
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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


def hours_between(a, b):
    """
    Hours from ISO timestamp a to ISO timestamp b, as a float.
    """
    return seconds_between(a, b) / 3600


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


def in_weekly_window(seconds, window):
    """
    Whether a time, in seconds since 1970, falls in a weekly window given as
    (weekday, Monday 0, start hour, end hour) in US Eastern time, which keeps
    daylight saving time as the venues' schedules do.
    """
    weekday, start, end = window
    eastern = datetime.fromtimestamp(seconds, timezone.utc).astimezone(EASTERN)
    return eastern.weekday() == weekday and start <= eastern.hour < end


def written_date(text):
    """
    A date written out, 'October 4, 2026', 'Oct 4, 2026', or 'Sept. 4 2026', as YYYY-MM-DD, or None.
    """
    plain = re.sub(r"^Sept\b", "Sep", text.replace(".", "").replace(",", ""))       # 'Sept' alone, not September's start.
    for form in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(plain, form).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def last_day(day, hour=None, minute=None, half=None):
    """
    The last day, YYYY-MM-DD, that a deadline on day at hour:minute, AM or
    PM as half says, leaves whole: the day before at 12:00 AM, when the day
    has not begun, and the day itself at any other time or with none, as in
    'before Sep 1, 2026 at 12:00 AM ET' and 'by Dec 31, 2026 at 11:59 PM ET'.
    """
    midnight = hour is not None and int(hour) == 12 and int(minute) == 0 and half.upper() == "AM"
    return shift(f"{day}T00:00:00+00:00", days=-1)[:10] if midnight else day


CALENDAR_SEASONS = {"mlb"}  # Sports whose season ends in the year it starts.


def season_from_date(game_date, sport=None):
    """
    Season end year for a game date of the sport. A football game from August
    onward belongs to the season ending next year, while a baseball season
    ends in the year it starts.
    """
    year, month = int(game_date[:4]), int(game_date[5:7])
    return year + 1 if month >= 8 and sport not in CALENDAR_SEASONS else year


def add_business_days(iso_time, days):
    """
    The same time of day this many weekdays later, skipping Saturdays and Sundays.
    """
    when = datetime.fromisoformat(iso_time)
    while days > 0:
        when += timedelta(days=1)
        if when.weekday() < 5:
            days -= 1
    return when.isoformat()
