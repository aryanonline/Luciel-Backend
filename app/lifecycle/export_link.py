"""The export download link the owner receives (§3.6.6 / §5.10, audit F028).

A presigned S3 URL is only valid while the credentials that signed it are, and an
ECS task role's temporary credentials rotate within hours — so the "7-day link" the
product promised died the same day. The link is now a signed token on the API:
``{api}/api/v1/exports/{token}``. The token carries the tenant and export id, lives
``export_link_ttl_days``, and every visit mints a fresh, minutes-long presign for the
object (whose own bucket lifecycle expires it a day after the link).
"""

from __future__ import annotations

import uuid

from app.auth.tokens import decode_token, mint_link_token
from app.config import get_settings

EXPORT_TOKEN_PURPOSE = "export"  # noqa: S105 — a token PURPOSE label, not a secret
#: How long each minted presign lives — one download, not a standing door.
PRESIGN_TTL_SECONDS = 15 * 60


class ExportLinkRejected(Exception):
    """The token is missing, malformed, expired, or not an export token."""


def api_base_url() -> str:
    """The public API origin the link must point at (never the frontend origin)."""
    settings = get_settings()
    return (settings.public_api_base_url or settings.app_base_url).rstrip("/")


def mint_export_token(admin_id: uuid.UUID, export_id: uuid.UUID, *, ttl_days: int) -> str:
    return mint_link_token(admin_id, EXPORT_TOKEN_PURPOSE, ttl_days * 24 * 3600, str(export_id))


def export_download_url(admin_id: uuid.UUID, export_id: uuid.UUID, *, ttl_days: int) -> str:
    token = mint_export_token(admin_id, export_id, ttl_days=ttl_days)
    return f"{api_base_url()}/api/v1/exports/{token}"


def parse_export_token(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    """(admin_id, export_id) from a valid export token; raises ExportLinkRejected."""
    try:
        claims = decode_token(token)
    except Exception as exc:  # noqa: BLE001 — every decode failure is the same to the visitor
        raise ExportLinkRejected("invalid or expired") from exc
    if claims.get("typ") != EXPORT_TOKEN_PURPOSE:
        raise ExportLinkRejected("not an export token")
    try:
        return uuid.UUID(str(claims["sub"])), uuid.UUID(str(claims["jti"]))
    except (KeyError, ValueError) as exc:
        raise ExportLinkRejected("malformed") from exc
