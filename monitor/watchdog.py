"""Hang watchdog for the monitor daemon.

On 4 Nov the daemon froze inside a check at 07:10 Madrid and stayed silent
for 5.5 hours: the process was alive, so neither the mutex nor the Task
Scheduler noticed, and the whole working day went unchecked. Planned sleeps go
through sleep() here and every log line counts as activity, so a long silence
can only come from a call that hung.

The Playwright sync API cannot be unblocked from another thread, so the
watchdog does not try to recover in place: it dumps every thread's stack to
the log (to find the call that hung), releases the singleton port, starts a
fresh copy of the daemon with the same arguments and kills this process.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("monitor.watchdog")

_last_activity = time.monotonic()
_lock = threading.Lock()

# Windows process creation flags: no console, own process group, so the
# replacement survives this process being killed.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


class _ActivityHandler(logging.Handler):
    """Treats every log record as a sign of life."""

    def emit(self, record: logging.LogRecord) -> None:
        global _last_activity
        with _lock:
            _last_activity = time.monotonic()


def beat() -> None:
    """Record a sign of life without writing a log line."""
    global _last_activity
    with _lock:
        _last_activity = time.monotonic()


def sleep(seconds: float) -> None:
    """Planned sleep that keeps the watchdog fed, so only real work can trip it."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        beat()
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(60.0, left))


def _silence_seconds() -> float:
    with _lock:
        return time.monotonic() - _last_activity


def _dump_stacks(log_path: Path) -> None:
    """Append all thread stacks to the log file."""
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("----- watchdog: thread stacks -----\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.write("----- end of stacks -----\n")
    except OSError as exc:
        logger.warning("Could not dump stacks: %s", exc)


def _respawn(release: Callable[[], None]) -> None:
    """Start a fresh daemon with the same command line, then exit hard."""
    release()
    cmd = [sys.executable, *sys.argv]
    try:
        subprocess.Popen(  # noqa: S603 - our own interpreter and arguments
            cmd,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        logger.error("Watchdog: replacement daemon started; exiting the hung one")
    except OSError as exc:
        logger.error("Watchdog: could not start a replacement (%s); exiting anyway", exc)
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except (OSError, ValueError):
            pass
    os._exit(3)


def start(
    limit_minutes: float,
    log_path: Path,
    release: Callable[[], None],
    alert: Optional[Callable[[str], object]] = None,
) -> None:
    """Start the watchdog thread.

    Args:
        limit_minutes: Log silence after which the process counts as hung.
            Must exceed the longest quiet stretch inside one check.
        log_path: Log file that receives the stack dump.
        release: Frees the singleton port so the replacement can claim it.
        alert: Optional callback with a short message (e.g. Telegram).
    """
    logging.getLogger().addHandler(_ActivityHandler())
    limit = limit_minutes * 60

    def _run() -> None:
        while True:
            time.sleep(30)
            silent = _silence_seconds()
            if silent < limit:
                continue
            minutes = round(silent / 60)
            logger.error("Watchdog: no log activity for %d min - the daemon is hung, restarting", minutes)
            _dump_stacks(log_path)
            if alert is not None:
                try:
                    alert(f"Cita monitor: завис ({minutes} мин без активности), перезапускаюсь.")
                except (OSError, RuntimeError, ValueError) as exc:  # an alert failure must not stop the restart
                    logger.warning("Watchdog alert failed: %s", exc)
            _respawn(release)

    threading.Thread(target=_run, name="watchdog", daemon=True).start()
    logger.info("Watchdog armed: restart after %d min of silence", limit_minutes)
