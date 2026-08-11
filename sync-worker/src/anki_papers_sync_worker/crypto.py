from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_version: int


def load_keys(value: str | None = None) -> dict[int, bytes]:
    raw = (value if value is not None else os.environ.get("ANKI_CREDENTIAL_KEY", "")).strip()
    if not raw:
        raise RuntimeError("ANKI_CREDENTIAL_KEY is required")
    keys: dict[int, bytes] = {}
    for entry in raw.split(","):
        version_text, encoded = entry.strip().split(":", 1) if ":" in entry else ("1", entry.strip())
        try:
            version = int(version_text)
            key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid ANKI_CREDENTIAL_KEY") from exc
        if version < 1 or len(key) != 32 or version in keys:
            raise RuntimeError("Invalid ANKI_CREDENTIAL_KEY")
        keys[version] = key
    return keys


def aad(user_id: int, version: int, field: str) -> bytes:
    return f"anki-papers:user:{user_id}:v:{version}:{field}".encode()


def encrypt(value: str | bytes, user_id: int, field: str, keys: dict[int, bytes]) -> EncryptedValue:
    version = max(keys)
    nonce = os.urandom(12)
    plaintext = value.encode() if isinstance(value, str) else value
    ciphertext = AESGCM(keys[version]).encrypt(nonce, plaintext, aad(user_id, version, field))
    return EncryptedValue(ciphertext, nonce, version)


def decrypt(value: EncryptedValue, user_id: int, field: str, keys: dict[int, bytes]) -> bytes:
    try:
        key = keys[value.key_version]
    except KeyError as exc:
        raise RuntimeError("Missing credential key version") from exc
    return AESGCM(key).decrypt(
        value.nonce,
        value.ciphertext,
        aad(user_id, value.key_version, field),
    )
