"""Password hashing — argon2id only (Architecture §3.7.1a, never plaintext/reversible)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()  # argon2id by default


def hash_password(plaintext: str) -> str:
    return _ph.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    try:
        return _ph.verify(stored_hash, plaintext)
    except VerifyMismatchError:
        return False
    except Exception:
        return False
