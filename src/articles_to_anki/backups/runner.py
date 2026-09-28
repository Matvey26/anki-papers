"""One backup attempt; temporary snapshots never accumulate on the server."""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from .backend import BackupBackend, create_backend
from .services import stopped_writers
from .snapshot import build_snapshot

LOGGER = logging.getLogger(__name__)


def run_backup(
    data_dir: Path, backend: BackupBackend, *, config_file: Path | None = None,
    pause_writers: bool = False
) -> dict[str, str]:
    if not backend.enabled:
        return {"status": "skipped", "reason": "backend_disabled"}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"anki-papers-{stamp}-{uuid.uuid4().hex[:8]}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="anki-papers-backup-") as temporary:
        with stopped_writers() if pause_writers else nullcontext():
            archive = build_snapshot(data_dir, Path(temporary), config_file=config_file)
        backend.upload(archive, name)
    return {"status": "succeeded", "name": name}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Back up all persistent Anki Papers data.")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ANKI_PAPERS_DATA_DIR", "data")))
    parser.add_argument("--backend", default=os.environ.get("ANKI_PAPERS_BACKUP_BACKEND", "dummy"))
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--pause-writers", action="store_true", help="Stop systemd writers only while taking the snapshot (requires root).")
    args = parser.parse_args(argv)
    try:
        result = run_backup(args.data_dir, create_backend(args.backend), config_file=args.config_file, pause_writers=args.pause_writers)
    except Exception as exc:
        # Provider errors may contain tokens, URLs, or credentials.
        LOGGER.error("Backup failed (%s)", type(exc).__name__)
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
