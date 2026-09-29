"""
Check that live trading can reach its accounts and its alerts, without trading.

Reads each venue's balance with the keys in data/, the same calls the live
executor makes every 15 seconds, and says when the Kalshi key's location
attestation lapses: past it, Kalshi no longer takes the key for sports
markets, so it must be renewed on Kalshi before then. With --email it
also sends a test email through data/email.json, the way live alerts go
out. Nothing is traded. Run it on the instance before a live run.

Run from src/ with:
    python3 -m tools.live_check
    python3 -m tools.live_check --email
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from api import kalshi
from engine.components.money.live import READERS
from engine.components.trading import notify

RENEW_DAYS = 7      # Say to renew the Kalshi key's location attestation once it lapses in fewer days than this.


def check_balances():
    """
    Print each venue's balance, or why it could not be read. Returns whether every venue was read.
    """
    ok = True
    for venue, read in READERS.items():
        try:
            print(f"{venue}: {read():,.2f}$ available to trade")
        except Exception as e:
            print(f"{venue}: the balance could not be read ({e!r})")
            ok = False
    return ok


def check_kalshi_key(read=kalshi.attestation_lapses, now=time.time):
    """
    Print when the Kalshi key's location attestation lapses, and to renew it
    when that is near. Returns whether it has not lapsed.
    """
    try:
        lapses = read()
    except Exception as e:
        print(f"kalshi key: its location attestation could not be read ({e!r})")
        return False
    if not lapses:
        print("kalshi key: Kalshi gives no date for its location attestation")
        return True
    days = (lapses - now()) / 86400
    when = datetime.fromtimestamp(lapses, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if days <= 0:
        print(f"kalshi key: its location attestation lapsed at {when}, so Kalshi refuses it for sports markets until it is renewed on Kalshi")
        return False
    print(f"kalshi key: location attestation good until {when}, in {days:.1f} days" + (", renew it on Kalshi before then" if days < RENEW_DAYS else ""))
    return True


def check_email():
    """
    Send a test email through the alert settings. Returns whether it went out.
    """
    try:
        settings = json.loads(notify.EMAIL_FILE.read_text())
        notify.send_email(settings, "SportsArb test email", "Live alerts will reach you here.")
    except Exception as e:
        print(f"email: not sent ({e!r})")
        return False
    print(f"email: sent to {', '.join(settings['to'])}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Check the live accounts and alerts without trading.")
    ap.add_argument("--email", action="store_true", help="also send a test email through data/email.json")
    args = ap.parse_args()
    ok = check_balances()
    ok = check_kalshi_key() and ok
    if args.email:
        ok = check_email() and ok
    sys.exit(0 if ok else 1)
