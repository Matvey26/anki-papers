"""Briefly quiesce service writers while a consistent snapshot is assembled."""
from __future__ import annotations

import signal
import subprocess
from contextlib import contextmanager

WRITERS = ("anki-papers-web.service", "anki-papers-sync-worker.service")


@contextmanager
def stopped_writers():
    """Restore previously active services on success, failure, or termination."""
    active = [unit for unit in WRITERS if subprocess.run(
        ["systemctl", "is-active", "--quiet", unit], check=False
    ).returncode == 0]
    if not active:
        yield
        return

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        subprocess.run(["systemctl", "stop", *active], check=True)
        yield
    finally:
        try:
            subprocess.run(["systemctl", "start", *active], check=True)
        finally:
            signal.signal(signal.SIGTERM, previous)
