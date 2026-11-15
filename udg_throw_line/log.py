"""Minimal logging to stderr (same scheme as udg-leds-service).

The stdlib `logging` package is deliberately not used: in OpenEmbedded it is a
separate package (python3-logging) that pulls threading, pickle, netserver...
for nothing. Under systemd, stderr goes to the journal, which understands the
sd-daemon "<N>" priority prefix, so `journalctl -p warning` still works.
"""

import os
import sys
import time

ERROR = 3
WARNING = 4
NOTICE = 5
INFO = 6
DEBUG = 7

_NAMES = {ERROR: "error", WARNING: "warning", NOTICE: "notice", INFO: "info", DEBUG: "debug"}


def _stderr_is_journal():
    # systemd sets JOURNAL_STREAM=<dev>:<inode> when stdout/stderr go to the
    # journal; the variable is inherited by children whose stderr may be
    # elsewhere, hence the comparison recommended by systemd.exec(5).
    value = os.environ.get("JOURNAL_STREAM", "")
    try:
        dev, ino = (int(part) for part in value.split(":"))
        st = os.fstat(sys.stderr.fileno())
    except (ValueError, OSError, AttributeError):
        return False
    return st.st_dev == dev and st.st_ino == ino


class _State:
    level = INFO
    # Under the journal, lines are timestamped by journald and "<N>" sets
    # their priority; on a terminal we print our own time and level.
    journal = _stderr_is_journal()
    stream = None


def configure(verbose=None, stream=None, journal=None):
    if verbose is not None:
        _State.level = DEBUG if verbose else INFO
    if stream is not None:
        _State.stream = stream
    if journal is not None:
        _State.journal = journal


def log(priority, message):
    if priority > _State.level:
        return
    stream = _State.stream or sys.stderr
    if _State.journal:
        line = "<%d>%s\n" % (priority, message)
    else:
        line = "%s %s: %s\n" % (time.strftime("%H:%M:%S"), _NAMES.get(priority, "log"), message)
    try:
        stream.write(line)
        stream.flush()
    except (OSError, ValueError):
        # A closed or broken stderr must never take the service down.
        pass


def error(message):
    log(ERROR, message)


def warning(message):
    log(WARNING, message)


def notice(message):
    log(NOTICE, message)


def info(message):
    log(INFO, message)


def debug(message):
    log(DEBUG, message)


class Once:
    """Logs a recurring condition once, then again only when it changes.

    The camera and the laser output are retried every few seconds while they
    are missing; repeating the same error every time would bury the journal.
    """

    def __init__(self):
        self.last = None

    def __call__(self, logger, message):
        if message != self.last:
            self.last = message
            logger(message)

    def reset(self):
        self.last = None
