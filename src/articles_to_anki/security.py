from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PASSWORD_MIN_LENGTH = 12
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError("Пароль должен содержать не менее 12 символов.")
    if len(password) > 1024:
        raise ValueError("Пароль слишком длинный.")


def claim_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_version: int


def load_credential_keys(value: str | None = None) -> dict[int, bytes]:
    """Load `version:base64key` entries; a lone key means version 1."""
    raw = (value if value is not None else os.environ.get("ANKI_CREDENTIAL_KEY", "")).strip()
    if not raw:
        return {}
    keys: dict[int, bytes] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        version_text, encoded = entry.split(":", 1) if ":" in entry else ("1", entry)
        try:
            version = int(version_text)
            key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError) as exc:
            raise ValueError("ANKI_CREDENTIAL_KEY has invalid format") from exc
        if version < 1 or len(key) != 32:
            raise ValueError("ANKI_CREDENTIAL_KEY must contain 32-byte AES-256 keys")
        if version in keys:
            raise ValueError("ANKI_CREDENTIAL_KEY repeats a key version")
        keys[version] = key
    return keys


def associated_data(user_id: int, key_version: int, field: str) -> bytes:
    return f"anki-papers:user:{user_id}:v:{key_version}:{field}".encode()


def encrypt_value(
    plaintext: str | bytes,
    *,
    user_id: int,
    field: str,
    keys: dict[int, bytes],
) -> EncryptedValue:
    if not keys:
        raise RuntimeError("ANKI_CREDENTIAL_KEY is not configured")
    version = max(keys)
    nonce = os.urandom(12)
    value = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    ciphertext = AESGCM(keys[version]).encrypt(
        nonce,
        value,
        associated_data(user_id, version, field),
    )
    return EncryptedValue(ciphertext, nonce, version)


def decrypt_value(
    encrypted: EncryptedValue,
    *,
    user_id: int,
    field: str,
    keys: dict[int, bytes],
) -> bytes:
    try:
        key = keys[encrypted.key_version]
    except KeyError as exc:
        raise RuntimeError("Required credential key version is unavailable") from exc
    return AESGCM(key).decrypt(
        encrypted.nonce,
        encrypted.ciphertext,
        associated_data(user_id, encrypted.key_version, field),
    )
