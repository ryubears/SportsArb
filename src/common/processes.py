"""
The child processes the live process starts: each venue's feed, see
market/feeds.py, and each catalog refresh, see engine/run.py.
"""

import multiprocessing
import signal
import sys

CONTEXT = multiprocessing.get_context("spawn")  # A child starts afresh, not as a copy of a process with a running loop and threads.


def set_up_child():
    """
    What a child does before its work.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)    # The main process stops its children, Ctrl-C included.
    sys.stdout.reconfigure(line_buffering=True)     # Log lines go out as they are written, as the main process's do.
