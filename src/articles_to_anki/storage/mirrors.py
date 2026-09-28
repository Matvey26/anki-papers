"""storage / mirrors."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from articles_to_anki.constants import LOGGER
from articles_to_anki.security import (
    EncryptedValue,
    decrypt_value,
    load_credential_keys,
)


def decrypt_mirror_collection(
    database: sqlite3.Connection, user_id: int
) -> bytes | None:
    """Return the stored AnkiWeb collection mirror, or None when unavailable."""
    account = database.execute(
        "SELECT mirror_path, mirror_nonce, mirror_key_version FROM anki_accounts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not account or not account["mirror_path"]:
        return None
    mirror = Path(account["mirror_path"])
    if not mirror.is_file():
        return None
    try:
        return decrypt_value(
            EncryptedValue(
                mirror.read_bytes(),
                account["mirror_nonce"],
                int(account["mirror_key_version"]),
            ),
            user_id=user_id,
            field="collection_mirror",
            keys=load_credential_keys(),
        )
    except Exception:
        LOGGER.warning("Could not decrypt collection mirror for user %s", user_id)
        return None
