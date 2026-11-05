"""PKCE (RFC 7636, S256) and the random secrets of one sign-in."""

import base64
import hashlib
import secrets


def new_secret() -> str:
    """A URL-safe random value with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    # 64 random bytes -> 86 characters, inside RFC 7636's 43..128.
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
