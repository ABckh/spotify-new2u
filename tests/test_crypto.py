"""AES-256-GCM token encryption and keyed hashes."""

from typing import get_args
from uuid import uuid4

import pytest

from app.core.crypto import (
    DecryptionError,
    HashPurpose,
    decrypt_secret,
    encrypt_secret,
    keyed_hash,
)
from app.spotify.connection import decrypt_refresh_token, encrypt_refresh_token

SECRET = "synthetic-refresh-token-5b1e"


def test_round_trip_and_no_plaintext_in_the_stored_value() -> None:
    stored = encrypt_secret(SECRET, "aad")

    assert SECRET.encode() not in stored
    assert decrypt_secret(stored, "aad") == SECRET


def test_each_encryption_uses_a_fresh_nonce() -> None:
    assert encrypt_secret(SECRET, "aad") != encrypt_secret(SECRET, "aad")


def test_a_token_copied_to_another_user_does_not_decrypt() -> None:
    owner, other = uuid4(), uuid4()
    stored = encrypt_refresh_token(owner, SECRET)

    assert decrypt_refresh_token(owner, stored) == SECRET
    with pytest.raises(DecryptionError):
        decrypt_refresh_token(other, stored)


def test_a_tampered_value_does_not_decrypt() -> None:
    stored = bytearray(encrypt_secret(SECRET, "aad"))
    stored[-1] ^= 0x01

    with pytest.raises(DecryptionError) as raised:
        decrypt_secret(bytes(stored), "aad")
    assert SECRET not in str(raised.value)


def test_a_truncated_value_raises_decryption_error() -> None:
    stored = encrypt_secret(SECRET, "aad")

    for length in (1, 5, 13, 28):
        with pytest.raises(DecryptionError):
            decrypt_secret(stored[:length], "aad")


def test_keyed_hash_is_stable_and_not_the_secret() -> None:
    assert keyed_hash(SECRET, purpose="session") == keyed_hash(SECRET, purpose="session")
    assert keyed_hash(SECRET, purpose="session") != keyed_hash(SECRET + "x", purpose="session")
    assert SECRET.encode() not in keyed_hash(SECRET, purpose="session")


def test_keyed_hashes_for_different_purposes_differ() -> None:
    purposes: tuple[HashPurpose, ...] = get_args(HashPurpose)
    hashes = {keyed_hash(SECRET, purpose=purpose) for purpose in purposes}

    assert len(hashes) == len(purposes) == 5
