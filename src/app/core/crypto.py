"""Encryption at rest for provider tokens, and keyed hashes for our own secrets.

**Encryption.** AES-256-GCM (an AEAD) from ``cryptography``, with the 32-byte
key in TOKEN_ENCRYPTION_KEY (URL-safe base64). Stored value::

    version (1 byte, 0x01) || nonce (12 random bytes) || ciphertext || tag (16 bytes)

Every call takes an ``aad`` (associated data) naming what the secret is and
whose it is, e.g. ``spotify_refresh_token:<user_id>``. Decryption fails unless
the same ``aad`` is given, so a ciphertext copied onto another user's row, or
into another column, does not decrypt.

**Keyed hashes.** Session tokens, refresh tokens, claim secrets, callback codes
and OAuth ``state`` values are never stored; their HMAC-SHA256 under SESSION_TOKEN_SECRET
is, with the purpose prefixed to the message. A database dump alone therefore
cannot be replayed as a session.

Rotating TOKEN_ENCRYPTION_KEY or SESSION_TOKEN_SECRET: see docs/self-hosting.md.

Nothing here logs its inputs or outputs, and errors never include them.
"""

import base64
import hashlib
import hmac
import os
from functools import lru_cache
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_VERSION = b"\x01"
_NONCE_BYTES = 12
_TAG_BYTES = 16


class DecryptionError(Exception):
    """The stored value is not one we encrypted for this ``aad`` with this key."""


@lru_cache(maxsize=1)
def _aead() -> AESGCM:
    encoded = get_settings().token_encryption_key
    if not encoded:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except ValueError:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not URL-safe base64") from None
    if len(key) != 32:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must decode to 32 bytes")
    return AESGCM(key)


def encrypt_secret(plaintext: str, aad: str) -> bytes:
    """Encrypt ``plaintext`` bound to ``aad``. Returns the stored bytes."""
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _aead().encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return _VERSION + nonce + sealed


def decrypt_secret(stored: bytes, aad: str) -> str:
    """Decrypt a value from :func:`encrypt_secret`. Raises ``DecryptionError``."""
    if stored[:1] != _VERSION:
        raise DecryptionError("unknown ciphertext version")
    if len(stored) < 1 + _NONCE_BYTES + _TAG_BYTES:
        # Truncated: AESGCM would raise ValueError, which callers do not expect.
        raise DecryptionError("ciphertext is too short")
    nonce, sealed = stored[1 : 1 + _NONCE_BYTES], stored[1 + _NONCE_BYTES :]
    try:
        return _aead().decrypt(nonce, sealed, aad.encode("utf-8")).decode("utf-8")
    except InvalidTag:
        raise DecryptionError("ciphertext does not authenticate") from None


# What a keyed hash is for. Part of the hashed message, so a hash made for one
# purpose never matches a lookup for another (domain separation, WHO-36 F6).
HashPurpose = Literal["state", "claim", "callback", "session", "refresh"]


def keyed_hash(secret: str, *, purpose: HashPurpose) -> bytes:
    """HMAC-SHA256 of ``purpose:secret`` under SESSION_TOKEN_SECRET, for storage and lookup."""
    key = get_settings().session_token_secret
    if not key:
        raise RuntimeError("SESSION_TOKEN_SECRET is not set")
    message = f"{purpose}:{secret}".encode()
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()
