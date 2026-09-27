"""
Check that live trading can reach its accounts and its alerts, without trading.

Reads each venue's balance with the keys in data/, the same calls the live
executor makes every 30 seconds, and with --email sends a test email
through data/email.json, the way live alerts go out. Nothing is traded.
Run it on the instance before the first live run.

Run from src/ with:
    python3 -m tools.live_check
    python3 -m tools.live_check --email
"""

import argparse
import json
import sys
from live.components import accounts, notify


def check_balances():
    """
    Print each venue's balance, or why it could not be read. Returns whether every venue was read.
    """
    ok = True
    for venue, read in accounts.READERS.items():
        try:
            print(f"{venue}: {read():,.2f}$ available to trade")
        except Exception as e:
            print(f"{venue}: the balance could not be read ({e!r})")
            ok = False
    return ok


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
    if args.email:
        ok = check_email() and ok
    sys.exit(0 if ok else 1)
