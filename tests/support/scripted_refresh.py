"""
Catalog refreshes that need no venue, so a refresh can run in a child process in tests.

They live here rather than in a test file because a child process imports
them by name, from the import path pytest gives tests/support.
"""

import os
import time


def refresh(sports, log):
    """
    Says which process it ran in.
    """
    log(f"refreshing {', '.join(sports)}")
    return f"refreshed {', '.join(sports)} in process {os.getpid()}"


def crash(sports, log):
    """
    Dies without an answer, as a process the system kills does.
    """
    os._exit(3)


def slow(sports, log):
    """
    Takes a minute, longer than any test waits.
    """
    time.sleep(60)
    return "done"
