from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .crypto import EncryptedValue, decrypt, encrypt, load_keys
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


class SyncWorker:
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
        with self.connect_database() as database:
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

    def _decrypt_login(self, row: sqlite3.Row) -> tuple[str, str]:
        user_id = int(row["user_id"])
        version = int(row["key_version"])
        username = decrypt(
            EncryptedValue(row["ankiweb_id_ciphertext"], row["ankiweb_id_nonce"], version),
            user_id, "ankiweb_id", self.keys,
        ).decode()
        password = decrypt(
            EncryptedValue(row["password_ciphertext"], row["password_nonce"], version),
            user_id, "ankiweb_password", self.keys,
        ).decode()
        return username, password

    def _decrypt_hkey(self, row: sqlite3.Row) -> str:
        if row["hkey_ciphertext"] is None:
            raise AuthenticationError("missing_hkey")
        user_id = int(row["user_id"])
        return decrypt(
            EncryptedValue(
                row["hkey_ciphertext"], row["hkey_nonce"], int(row["key_version"])
            ),
            user_id,
            "ankiweb_hkey",
            self.keys,
        ).decode()

    def _store_credentials(
        self, database: sqlite3.Connection, user_id: int, username: str, password: str, hkey: str
    ) -> None:
        encrypted_id = encrypt(username, user_id, "ankiweb_id", self.keys)
        encrypted_password = encrypt(password, user_id, "ankiweb_password", self.keys)
        encrypted_hkey = encrypt(hkey, user_id, "ankiweb_hkey", self.keys)
        database.execute(
            """UPDATE user_credentials SET ankiweb_id_ciphertext = ?, ankiweb_id_nonce = ?,
               password_ciphertext = ?, password_nonce = ?, hkey_ciphertext = ?, hkey_nonce = ?,
               key_version = ?, state = 'active', auth_failures = 0, updated_at = ?
               WHERE user_id = ?""",
            (
                encrypted_id.ciphertext, encrypted_id.nonce,
                encrypted_password.ciphertext, encrypted_password.nonce,
                encrypted_hkey.ciphertext, encrypted_hkey.nonce,
                encrypted_id.key_version, now(), user_id,
            ),
        )

    def _store_mirror(self, user_id: int, collection_path: Path) -> tuple[Path, bytes, int]:
        encrypted = encrypt(collection_path.read_bytes(), user_id, "collection_mirror", self.keys)
        destination = self.mirror_root / f"{user_id}.anki2.enc"
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(encrypted.ciphertext)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination, encrypted.nonce, encrypted.key_version

    def _restore_mirror(self, account: sqlite3.Row, user_id: int, destination: Path) -> None:
        mirror = Path(account["mirror_path"]).resolve()
        if self.mirror_root not in mirror.parents:
            raise PermanentSyncError("invalid_mirror_path")
        plaintext = decrypt(
            EncryptedValue(mirror.read_bytes(), account["mirror_nonce"], int(account["mirror_key_version"])),
            user_id, "collection_mirror", self.keys,
        )
        destination.write_bytes(plaintext)
        os.chmod(destination, 0o600)

    @staticmethod
    def _cards(database: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
        rows = database.execute(
            """SELECT cards.*, documents.name AS document_name FROM cards
               JOIN documents ON documents.id = cards.document_id
               WHERE cards.user_id = ? ORDER BY cards.created_at""",
            (user_id,),
        ).fetchall()
        return [
            {
                "id": row["id"], "target": row["target"], "sentence": row["sentence"],
                "replacement": row["replacement"],
                "translations": json.loads(row["translations_json"]),
                "alternatives": json.loads(row["alternatives_json"]),
                "document_name": row["document_name"], "page": row["page"],
                "semantic": bool(row["semantic_version"]),
                "lemma": row["lemma"],
                "family_key": row["family_key"],
                "part_of_speech": row["part_of_speech"],
                "sense_definition_en": row["sense_definition_en"],
                "contexts": json.loads(row["contexts_json"] or "[]"),
            }
            for row in rows
        ]

    @staticmethod
    def _links(database: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in database.execute(
                """SELECT site_card_id, direction, note_id, note_guid
                   FROM anki_note_links WHERE user_id = ?""",
                (user_id,),
            ).fetchall()
        ]

    @staticmethod
    def _replace_links(
        database: sqlite3.Connection, user_id: int, links: list[dict[str, Any]]
    ) -> None:
        timestamp = now()
        for link in links:
            database.execute(
                """INSERT INTO anki_note_links
                   (user_id, site_card_id, direction, note_id, note_guid, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, site_card_id, direction) DO UPDATE SET
                     note_id = excluded.note_id, note_guid = excluded.note_guid""",
                (
                    user_id, link["site_card_id"], link["direction"],
                    link["note_id"], link["note_guid"], timestamp,
                ),
            )

    def _auth_failed(self, database: sqlite3.Connection, job: sqlite3.Row) -> None:
        user_id = int(job["user_id"])
        row = database.execute(
            "SELECT auth_failures FROM user_credentials WHERE user_id = ?", (user_id,)
        ).fetchone()
        failures = (int(row[0]) if row else 0) + 1
        state = "needs_reconnect" if failures >= 2 else "pending"
        database.execute(
            "UPDATE user_credentials SET auth_failures = ?, state = ?, updated_at = ? WHERE user_id = ?",
            (failures, state, now(), user_id),
        )
        if failures >= 2:
            database.execute(
                "UPDATE anki_accounts SET state = 'needs_reconnect', last_error = ?, updated_at = ? WHERE user_id = ?",
                ("Нужно переподключить AnkiWeb.", now(), user_id),
            )
            self._finish_failed(database, job, "auth")
        else:
            self._retry_or_fail(database, job, "auth")

    def _retry_or_fail(self, database: sqlite3.Connection, job: sqlite3.Row, code: str) -> None:
        attempts = int(job["attempts"])
        if attempts >= MAX_ATTEMPTS:
            database.execute(
                """UPDATE anki_accounts SET state = 'error', last_error = ?, updated_at = ?
                   WHERE user_id = ?""",
                (
                    "Синхронизация не удалась после пяти попыток. Запустите повтор вручную.",
                    now(),
                    job["user_id"],
                ),
            )
            self._finish_failed(database, job, code)
            return
        delay = RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)]
        run_after = (datetime.now(UTC) + timedelta(minutes=delay)).isoformat()
        queued = database.execute(
            """SELECT id, attempts FROM sync_jobs
               WHERE user_id = ? AND state = 'queued' AND id != ? LIMIT 1""",
            (job["user_id"], job["id"]),
        ).fetchone()
        if queued:
            database.execute(
                """UPDATE sync_jobs SET attempts = ?, run_after = ?, error_code = ?, updated_at = ?
                   WHERE id = ?""",
                (max(attempts, int(queued["attempts"])), run_after, code, now(), queued["id"]),
            )
            database.execute(
                """UPDATE sync_jobs SET state = 'cancelled', finished_at = ?,
                   error_code = ?, updated_at = ? WHERE id = ?""",
                (now(), "coalesced", now(), job["id"]),
            )
        else:
            database.execute(
                """UPDATE sync_jobs SET state = 'queued', run_after = ?, error_code = ?, updated_at = ?
                   WHERE id = ?""",
                (run_after, code, now(), job["id"]),
            )
        database.execute(
            "UPDATE anki_accounts SET state = 'error', last_error = ?, updated_at = ? WHERE user_id = ?",
            ("Временная ошибка синхронизации; повтор запланирован.", now(), job["user_id"]),
        )
        database.commit()

    @staticmethod
    def _connection_exists(
        database: sqlite3.Connection, user_id: int, expected_credentials_updated_at: str
    ) -> bool:
        return bool(
            database.execute(
                """SELECT 1 FROM user_credentials
                   JOIN anki_accounts USING(user_id)
                   WHERE user_id = ? AND user_credentials.updated_at = ?""",
                (user_id, expected_credentials_updated_at),
            ).fetchone()
        )

    @staticmethod
    def _finish_success(database: sqlite3.Connection, job: sqlite3.Row) -> None:
        database.execute(
            "UPDATE sync_jobs SET state = 'succeeded', finished_at = ?, updated_at = ?, error_code = NULL WHERE id = ?",
            (now(), now(), job["id"]),
        )
        database.commit()

    @staticmethod
    def _finish_failed(database: sqlite3.Connection, job: sqlite3.Row, code: str) -> None:
        database.execute(
            "UPDATE sync_jobs SET state = 'failed', finished_at = ?, updated_at = ?, error_code = ? WHERE id = ?",
            (now(), now(), code, job["id"]),
        )
        database.commit()

    @staticmethod
    def _cancel_job(database: sqlite3.Connection, job: sqlite3.Row) -> None:
        database.execute(
            "UPDATE sync_jobs SET state = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
            (now(), now(), job["id"]),
        )
        database.commit()


def run_forever(worker: SyncWorker, poll_seconds: float = 2.0) -> None:
    worker.startup_cleanup()
    while True:
        worked = worker.run_once()
        if not worked:
            time.sleep(poll_seconds)


def permanent_error_message(code: str) -> str:
    return {
        "remote_collection_empty": (
            "Коллекция AnkiWeb пуста. Сначала загрузите её из Anki Desktop."
        ),
        "deck_not_selected": "Не выбрана целевая колода AnkiWeb.",
        "managed_notetype_invalid": (
            "Тип карточек «Anki Papers» изменён вручную и несовместим."
        ),
        "repeated_full_sync": "AnkiWeb несколько раз потребовал полную синхронизацию.",
    }.get(code, "Коллекция требует ручной проверки перед синхронизацией.")
