"""Persist sync credentials, mirrors, links, and job outcomes."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .crypto import EncryptedValue, decrypt, encrypt
from .official import (
    AuthenticationError,
    PermanentSyncError,
)

RETRY_MINUTES = (1, 5, 15, 60)
MAX_ATTEMPTS = 5


def now() -> str:
    return datetime.now(UTC).isoformat()



class SyncState:
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
