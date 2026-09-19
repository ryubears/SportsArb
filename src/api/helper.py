"""
Helper functions shared by the venue clients.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

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


def float_or_none(value):
    """
    Convert to float, or return None when the value is missing or not numeric.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
