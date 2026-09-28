from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .crypto import load_keys
from .official import (
    AuthenticationError,
    OfficialAnkiAdapter,
    PermanentSyncError,
    RetryableSyncError,
)

RETRY_MINUTES = (1, 5, 15, 60)
MAX_ATTEMPTS = 5


def now() -> str:
    return datetime.now(UTC).isoformat()


from .state import SyncState, permanent_error_message


class SyncWorker(SyncState):
    def __init__(
        self,
        database_path: Path,
        data_dir: Path,
        *,
        keys: dict[int, bytes] | None = None,
        adapter: Any | None = None,
        worker_name: str = "anki-sync-1",
    ) -> None:
        self.database_path = database_path.resolve()
        self.data_dir = data_dir.resolve()
        self.keys = keys or load_keys()
        self.adapter = adapter or OfficialAnkiAdapter()
        self.worker_name = worker_name
        self.work_root = self.data_dir / "anki_work"
        self.mirror_root = self.data_dir / "anki_mirrors"
        self.lock_root = self.data_dir / "anki_locks"
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mirror_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def startup_cleanup(self) -> None:
        for child in self.work_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def connect_database(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database_path, timeout=10)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        return database

    def heartbeat(self, database: sqlite3.Connection) -> None:
        database.execute(
            """INSERT INTO worker_heartbeat(worker_name, updated_at) VALUES (?, ?)
               ON CONFLICT(worker_name) DO UPDATE SET updated_at = excluded.updated_at""",
            (self.worker_name, now()),
        )
        database.commit()

    def run_once(self) -> bool:
        with closing(self.connect_database()) as database, database:
            self.heartbeat(database)
            job = self._claim_job(database)
            if job is None:
                return False
            try:
                with self._user_lock(int(job["user_id"])):
                    self._process(database, job)
            except AuthenticationError:
                database.rollback()
                self._auth_failed(database, job)
            except PermanentSyncError as exc:
                database.rollback()
                detail = str(exc)
                database.execute(
                    """UPDATE anki_accounts SET state = 'error', last_error = ?, updated_at = ?
                       WHERE user_id = ? AND state != 'needs_reconnect'""",
                    (permanent_error_message(detail), now(), job["user_id"]),
                )
                self._finish_failed(database, job, f"configuration:{detail}")
            except (RetryableSyncError, OSError, sqlite3.OperationalError) as exc:
                database.rollback()
                detail = str(exc) if isinstance(exc, RetryableSyncError) else type(exc).__name__
                self._retry_or_fail(database, job, f"temporary:{detail}")
            except Exception as exc:  # noqa: BLE001 - job boundary must contain unexpected failures
                database.rollback()
                self._retry_or_fail(database, job, f"internal:{type(exc).__name__}")
            return True

    @contextmanager
    def _user_lock(self, user_id: int):
        import fcntl

        lock_path = self.lock_root / f"{user_id}.lock"
        with lock_path.open("a+b") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _claim_job(self, database: sqlite3.Connection) -> sqlite3.Row | None:
        database.execute("BEGIN IMMEDIATE")
        job = database.execute(
            """SELECT * FROM sync_jobs
               WHERE state = 'queued' AND run_after <= ?
               ORDER BY run_after, created_at LIMIT 1""",
            (now(),),
        ).fetchone()
        if job is None:
            database.commit()
            return None
        database.execute(
            """UPDATE sync_jobs SET state = 'running', attempts = attempts + 1,
               started_at = ?, updated_at = ? WHERE id = ? AND state = 'queued'""",
            (now(), now(), job["id"]),
        )
        database.commit()
        return database.execute("SELECT * FROM sync_jobs WHERE id = ?", (job["id"],)).fetchone()

    def _process(self, database: sqlite3.Connection, job: sqlite3.Row) -> None:
        user_id = int(job["user_id"])
        credentials = database.execute(
            "SELECT * FROM user_credentials WHERE user_id = ?", (user_id,)
        ).fetchone()
        account = database.execute(
            "SELECT * FROM anki_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if credentials is None or account is None:
            self._cancel_job(database, job)
            return
        cards = self._cards(database, user_id)
        known_links = self._links(database, user_id)
        temporary = Path(tempfile.mkdtemp(prefix=f"user-{user_id}-", dir=self.work_root))
        os.chmod(temporary, 0o700)
        collection_path = temporary / "collection.anki2"
        try:
            if job["reason"] == "connect" or not account["mirror_path"]:
                username, password = self._decrypt_login(credentials)
                result = self.adapter.connect(
                    collection_path, username, password, cards, known_links
                )
                database.execute("BEGIN IMMEDIATE")
                if not self._connection_exists(database, user_id, credentials["updated_at"]):
                    self._cancel_job(database, job)
                    return
                self._store_credentials(database, user_id, username, password, result.hkey)
                mirror = self._store_mirror(user_id, collection_path)
                self._replace_links(database, user_id, result.links)
                database.execute(
                    """UPDATE anki_accounts SET state = 'awaiting_deck',
                       available_decks_json = ?, mirror_path = ?, mirror_nonce = ?,
                       mirror_key_version = ?, preview_existing = ?, preview_missing = ?,
                       last_error = NULL, updated_at = ? WHERE user_id = ?""",
                    (
                        json.dumps(result.decks, ensure_ascii=False),
                        str(mirror[0]), mirror[1], mirror[2],
                        result.existing, result.missing, now(), user_id,
                    ),
                )
                self._finish_success(database, job)
                return
            self._restore_mirror(account, user_id, collection_path)
            shutil.copy2(collection_path, temporary / "collection.backup.anki2")
            if job["reason"] == "rebuild_import":
                self._import_rebuild_job(
                    database, job, credentials, account, user_id, collection_path
                )
                return
            if not account["selected_deck_id"]:
                raise PermanentSyncError("deck_not_selected")
            database.execute(
                "UPDATE anki_accounts SET state = 'syncing', last_error = NULL, updated_at = ? WHERE user_id = ?",
                (now(), user_id),
            )
            database.commit()
            refreshed_hkey = None
            hkey = self._decrypt_hkey(credentials)
            try:
                result = self.adapter.sync(
                    collection_path,
                    hkey,
                    int(account["selected_deck_id"]),
                    cards,
                    known_links,
                )
            except AuthenticationError:
                username, password = self._decrypt_login(credentials)
                refreshed = self.adapter.login(collection_path, username, password)
                refreshed_hkey = refreshed
                result = self.adapter.sync(
                    collection_path,
                    refreshed,
                    int(account["selected_deck_id"]),
                    cards,
                    known_links,
                )
            database.execute("BEGIN IMMEDIATE")
            if not self._connection_exists(database, user_id, credentials["updated_at"]):
                self._cancel_job(database, job)
                return
            if refreshed_hkey is not None:
                self._store_credentials(
                    database, user_id, username, password, refreshed_hkey
                )
            mirror = self._store_mirror(user_id, collection_path)
            self._replace_links(database, user_id, result.links)
            timestamp = now()
            database.executemany(
                "UPDATE cards SET anki_synced_at = ? WHERE user_id = ? AND id = ?",
                [(timestamp, user_id, card["id"]) for card in cards],
            )
            database.execute(
                """UPDATE anki_accounts SET state = 'connected', available_decks_json = ?,
                   mirror_path = ?, mirror_nonce = ?, mirror_key_version = ?,
                   last_success_at = ?, last_error = NULL, last_added_count = ?, updated_at = ?
                   WHERE user_id = ?""",
                (
                    json.dumps(result.decks, ensure_ascii=False), str(mirror[0]), mirror[1], mirror[2],
                    timestamp, result.added, timestamp, user_id,
                ),
            )
            database.execute(
                "UPDATE user_credentials SET state = 'active', auth_failures = 0, updated_at = ? WHERE user_id = ?",
                (timestamp, user_id),
            )
            self._finish_success(database, job)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _import_rebuild_job(
        self,
        database: sqlite3.Connection,
        job: sqlite3.Row,
        credentials: sqlite3.Row,
        account: sqlite3.Row,
        user_id: int,
        collection_path: Path,
    ) -> None:
        """Push the latest successful rebuild into the AnkiWeb mirror."""
        rebuild = database.execute(
            """SELECT id, result_path FROM rebuild_jobs
               WHERE user_id = ? AND state = 'succeeded'
               ORDER BY created_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if rebuild is None or not rebuild["result_path"]:
            self._cancel_job(database, job)
            return
        apkg_path = Path(rebuild["result_path"]).resolve()
        if Path(self.data_dir / "rebuilds").resolve() not in apkg_path.parents:
            self._cancel_job(database, job)
            return
        database.execute(
            "UPDATE anki_accounts SET state = 'syncing', last_error = NULL, updated_at = ? WHERE user_id = ?",
            (now(), user_id),
        )
        database.commit()
        hkey = self._decrypt_hkey(credentials)
        refreshed_hkey = None
        try:
            result = self.adapter.import_rebuild(collection_path, apkg_path, hkey)
        except AuthenticationError:
            username, password = self._decrypt_login(credentials)
            refreshed_hkey = self.adapter.login(collection_path, username, password)
            result = self.adapter.import_rebuild(collection_path, apkg_path, refreshed_hkey)
        database.execute("BEGIN IMMEDIATE")
        if not self._connection_exists(database, user_id, credentials["updated_at"]):
            self._cancel_job(database, job)
            return
        if refreshed_hkey is not None:
            self._store_credentials(
                database, user_id, username, password, refreshed_hkey
            )
        mirror = self._store_mirror(user_id, collection_path)
        timestamp = now()
        database.execute(
            """UPDATE anki_accounts SET state = 'connected', available_decks_json = ?,
               mirror_path = ?, mirror_nonce = ?, mirror_key_version = ?,
               last_success_at = ?, last_error = NULL, last_added_count = 0, updated_at = ?
               WHERE user_id = ?""",
            (
                json.dumps(result.decks, ensure_ascii=False), str(mirror[0]), mirror[1], mirror[2],
                timestamp, timestamp, user_id,
            ),
        )
        database.execute(
            "UPDATE user_credentials SET state = 'active', auth_failures = 0, updated_at = ? WHERE user_id = ?",
            (timestamp, user_id),
        )
        database.execute(
            "UPDATE rebuild_jobs SET uploaded_to = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, rebuild["id"]),
        )
        self._finish_success(database, job)

def run_forever(worker: SyncWorker, poll_seconds: float = 2.0) -> None:
    worker.startup_cleanup()
    while True:
        worked = worker.run_once()
        if not worked:
            time.sleep(poll_seconds)
