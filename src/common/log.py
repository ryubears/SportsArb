"""
The one line logger the live process uses.
"""

from common.timeutil import now_iso


def log(message):
    """
    Print a message with the current UTC time in front.
    """
    print(f"{now_iso()[11:19]} {message}")
