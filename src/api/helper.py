"""
Helper functions shared by the venue clients.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

USER_AGENT = "SportsArb research (github.com/ryubears/SportsArb)"


def get_json(url, params=None, retries=3):
    """
    GET a URL and parse the JSON body. Retries a few times on network errors.
    """
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


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


def float_or_none(value):
    """
    Convert to float, or return None when the value is missing or not numeric.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def now_iso():
    """
    Current UTC time as an ISO 8601 string.
    """
    return datetime.now(timezone.utc).isoformat()
