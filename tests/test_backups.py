"""Backup snapshots must be recoverable; disabled storage must stay inert."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from articles_to_anki.backups.backend import DummyBackupBackend, create_backend
from articles_to_anki.backups.runner import run_backup
from articles_to_anki.backups.snapshot import build_snapshot


def test_dummy_does_not_touch_data_or_destination(tmp_path):
    backend = DummyBackupBackend()
    missing = tmp_path / "missing"
    assert run_backup(missing, backend) == {"status": "skipped", "reason": "backend_disabled"}
    backend.upload(missing, "ignored")
    assert backend.download("ignored", missing) is False
    assert backend.list_backups() == []
    backend.delete("ignored")
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        create_backend("typo")


def test_snapshot_includes_wal_and_all_persistent_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    live = sqlite3.connect(data / "app.sqlite3")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, value TEXT)")
    live.execute("INSERT INTO cards VALUES (1, 'committed WAL data')")
    live.commit()
    expected = {
        "uploads/user/source.pdf": b"source PDF",
        "uploads/user/deck.apkg": b"deck",
        "anki_mirrors/1.enc": b"encrypted mirror",
        "rebuilds/result.apkg": b"rebuild",
        "dictionaries/eng-rus/test.idx": b"dictionary",
        "new-feature/file.dat": b"future persistent data",
    }
    for name, content in expected.items():
        file = data / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    for directory in ("anki_work", "anki_locks", "backups"):
        (data / directory).mkdir()
        (data / directory / "excluded").write_text("transient or old backup")
    env = tmp_path / "runtime.env"
    env.write_text("ANKI_CREDENTIAL_KEY=test-recovery-key")
    staging = tmp_path / "staging"
    staging.mkdir()
    try:
        archive = build_snapshot(data, staging, config_file=env)
    finally:
        live.close()
    with tarfile.open(archive) as package:
        members = {member.name.removeprefix("./"): member for member in package.getmembers() if member.isfile()}
        assert not any(name.endswith(("-wal", "-shm")) for name in members)
        assert not any("excluded" in name for name in members)
        for name, content in expected.items():
            assert package.extractfile(members[f"data/{name}"]).read() == content
        assert package.extractfile(members["runtime.env"]).read() == env.read_bytes()
        manifest = json.load(package.extractfile(members["manifest.json"]))
        for entry in manifest["files"]:
            content = package.extractfile(members[entry["path"]]).read()
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(package.extractfile(members["data/app.sqlite3"]).read())
    with sqlite3.connect(restored) as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("SELECT value FROM cards").fetchone()[0] == "committed WAL data"


def test_snapshot_refuses_symlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "app.sqlite3").close()
    (data / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="Symlinks"):
        build_snapshot(data, tmp_path)


def test_failed_upload_is_not_success_and_temporary_files_are_removed(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "app.sqlite3").close()

    class BrokenStorage(DummyBackupBackend):
        enabled = True
        archive: Path | None = None

        def upload(self, archive, name):
            self.archive = archive
            assert archive.is_file()
            raise OSError("storage unavailable")

    backend = BrokenStorage()
    with pytest.raises(OSError, match="storage unavailable"):
        run_backup(data, backend)
    assert backend.archive is not None
    assert not backend.archive.parent.exists()


def test_success_is_reported_only_after_upload(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "app.sqlite3").close()

    class MemoryStorage(DummyBackupBackend):
        enabled = True

        def upload(self, archive, name):
            self.content = archive.read_bytes()
            self.name = name

    backend = MemoryStorage()
    result = run_backup(data, backend)
    assert result == {"status": "succeeded", "name": backend.name}
    assert backend.content.startswith(b"\x1f\x8b")


def test_writers_resume_even_when_snapshot_fails(monkeypatch):
    from types import SimpleNamespace

    from articles_to_anki.backups import services

    calls = []

    def systemctl(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(services.subprocess, "run", systemctl)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        with services.stopped_writers():
            raise RuntimeError("snapshot failed")
    assert calls[-2] == ["systemctl", "stop", *services.WRITERS]
    assert calls[-1] == ["systemctl", "start", *services.WRITERS]


def test_dummy_does_not_stop_writers(tmp_path, monkeypatch):
    from articles_to_anki.backups import services

    def forbidden(*args, **kwargs):
        pytest.fail("Dummy backup must not call systemctl")

    monkeypatch.setattr(services.subprocess, "run", forbidden)
    assert run_backup(tmp_path, DummyBackupBackend(), pause_writers=True)["status"] == "skipped"
