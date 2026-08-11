from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidTag

from articles_to_anki.security import (
    decrypt_value,
    encrypt_value,
    hash_password,
    load_credential_keys,
    verify_password,
)


def encoded_key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def test_argon2id_password_hash() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong password")


def test_aes_gcm_binds_user_field_and_key_version() -> None:
    keys = load_credential_keys(f"1:{encoded_key(1)},2:{encoded_key(2)}")
    encrypted = encrypt_value(
        "secret", user_id=7, field="ankiweb_password", keys=keys
    )
    second = encrypt_value("secret", user_id=7, field="ankiweb_password", keys=keys)
    assert encrypted.key_version == 2
    assert encrypted.nonce != second.nonce
    assert encrypted.ciphertext != second.ciphertext
    assert encrypted.ciphertext != b"secret"
    assert decrypt_value(
        encrypted, user_id=7, field="ankiweb_password", keys=keys
    ) == b"secret"
    with pytest.raises(InvalidTag):
        decrypt_value(encrypted, user_id=8, field="ankiweb_password", keys=keys)
    with pytest.raises(InvalidTag):
        decrypt_value(encrypted, user_id=7, field="ankiweb_hkey", keys=keys)


def test_old_key_can_decrypt_during_rotation() -> None:
    first = load_credential_keys(f"1:{encoded_key(1)}")
    encrypted = encrypt_value("secret", user_id=1, field="ankiweb_id", keys=first)
    rotated = load_credential_keys(f"1:{encoded_key(1)},2:{encoded_key(2)}")
    assert decrypt_value(encrypted, user_id=1, field="ankiweb_id", keys=rotated) == b"secret"
    assert encrypt_value("new", user_id=1, field="ankiweb_id", keys=rotated).key_version == 2
