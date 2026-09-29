"""Private, atomic OAuth state with an interprocess lock."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class OAuthStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def save(self, state: dict) -> None:
        fd, name = tempfile.mkstemp(prefix='.oauth-', dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w') as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(state, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
