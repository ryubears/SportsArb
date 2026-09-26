"""
The one line logger the live process uses, and helpers for logging errors with their tracebacks.
"""

import traceback
from common.timeutil import now_iso


def log(message):
    """
    Print a message with the current UTC time in front.
    """
    print(f"{now_iso()[11:19]} {message}")


def with_traceback(message, error):
    """
    The message followed by the error's traceback, indented under it, so an
    unexpected error in the log shows where it came from and not only its name.
    """
    lines = "".join(traceback.format_exception(error)).rstrip().splitlines()
    return message + "".join(f"\n    {line}" for line in lines)


def on_failure(log, what):
    """
    A done callback for a background task that logs its error with the
    traceback. Without it asyncio only mentions a failed task when the task
    is garbage collected, on stderr, if at all.
    """
    def callback(task):
        if not task.cancelled() and task.exception() is not None:
            log(with_traceback(f"{what} failed", task.exception()))
    return callback
