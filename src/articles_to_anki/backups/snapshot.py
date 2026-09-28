"""Portable snapshots of persistent data and recovery configuration."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

# Runtime scratch collections contain decrypted credentials/data. Mirrors are durable.
TRANSIENT_DIRECTORIES = {"anki_work", "anki_locks", "backups"}
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _copy_database(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
        with closing(sqlite3.connect(destination)) as snapshot:
            live.backup(snapshot)
            if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup database integrity check failed")


def _copy_file(source: Path, destination: Path) -> None:
    before = source.stat()
    shutil.copy2(source, destination)
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise RuntimeError("Data changed during backup; retry required")


def build_snapshot(data_dir: Path, staging: Path, *, config_file: Path | None = None) -> Path:
    """Build a checked archive in a private temporary directory.

    SQLite uses its online backup API (including committed WAL data). Other
    persistent files are included verbatim; a concurrent file change fails the
    run rather than silently accepting a partial file. For cross-file point-in-
    time consistency, invoke while writers are stopped.
    """
    data_dir = data_dir.resolve(strict=True)
    if not (data_dir / "app.sqlite3").is_file():
        raise FileNotFoundError("Service database is missing")
    payload = staging / "snapshot"
    payload.mkdir(mode=0o700)
    files = sorted(data_dir.rglob("*"))
    for source in files:
        relative = source.relative_to(data_dir)
        if relative.parts[0] in TRANSIENT_DIRECTORIES:
            continue
        if source.is_symlink():
            raise ValueError("Symlinks are not supported in backup data")
        if source.is_dir():
            (payload / "data" / relative).mkdir(parents=True, exist_ok=True)
            continue
        if source.name.endswith(SQLITE_SIDECARS):
            continue
        destination = payload / "data" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix in {".sqlite", ".sqlite3", ".anki2"}:
            _copy_database(source, destination)
        else:
            _copy_file(source, destination)
    if config_file is not None:
        if config_file.is_symlink():
            raise ValueError("Configuration must not be a symlink")
        _copy_file(config_file, payload / "runtime.env")
    manifest = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "source_data_dir": str(data_dir),
        "files": [],
    }
    for file in sorted(payload.rglob("*")):
        if file.is_file():
            with file.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest["files"].append({
                "path": file.relative_to(payload).as_posix(),
                "size": file.stat().st_size,
                "sha256": digest,
            })
    (payload / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = staging / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(payload, arcname=".")
    archive.chmod(0o600)
    return archive
