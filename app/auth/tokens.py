"""Session JWTs and single-use link tokens (Architecture §3.7.1a / §3.7.2).

Session token: short-lived JWT carrying admin_id + user_id, delivered via the
httpOnly cookie. It also carries a ``sid`` (session id) so a password reset can
revoke all sessions by bumping a per-account token epoch.

Verification & reset link tokens: signed JWTs with purpose + TTL. Single-use and
reissue-invalidation are enforced via a per-account ``token epoch`` / jti stored
in Postgres (handled by the auth service in Arc 9 auth wiring), not here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config import get_settings


def _now() -> datetime:
    return datetime.now(UTC)


def mint_session_token(admin_id: uuid.UUID, user_id: uuid.UUID, session_epoch: int) -> str:
    s = get_settings()
    payload = {
        "sub": str(admin_id),
        "uid": str(user_id),
        "ep": session_epoch,
        "typ": "session",
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(seconds=s.session_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_signing_key, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    s = get_settings()
    return jwt.decode(token, s.jwt_signing_key, algorithms=[s.jwt_algorithm])


def mint_link_token(admin_id: uuid.UUID, purpose: str, ttl_seconds: int, jti: str) -> str:
    """Verification / reset link token. ``jti`` ties it to a single-use record."""
    s = get_settings()
    payload = {
        "sub": str(admin_id),
        "typ": purpose,  # "verify" | "reset"
        "jti": jti,
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_signing_key, algorithm=s.jwt_algorithm)
