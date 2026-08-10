"""Per-tenant Microsoft token upkeep for the BYO Outlook mailbox (§3.1.6a / §3.8.5).

Microsoft access tokens live about an hour. Every other OAuth credential in the
platform (Meta's 60-day tokens, api_key rows) survives between health sweeps, so
nothing here ever needed a real refresher — ``ReconnectOnExpiry`` honestly reports
"no refresher exists" and the sweep marks the row expired. For a mailbox that would
mean reconnect-by-hand every hour, so this module supplies the real thing:

  * :func:`ensure_fresh_token` — the SEND/POLL paths' entry point: returns an access
    token that is good for at least the next few minutes, refreshing (and persisting
    the rotation) when needed. Returns ``None`` when it cannot, and the caller
    refuses honestly — a send through a dead token must fail as
    ``mailbox_reconnect_needed``, never look delivered.
  * :class:`MicrosoftTokenRefresher` — the same core behind the health worker's
    ``TokenRefresher`` protocol, so the scheduled sweep proactively rotates ahead of
    expiry (§3.8.5 steps 3–4) instead of the first send of the hour eating the cost.
  * :class:`ProviderDispatchingRefresher` — what the sweep binds: routes ``outlook``
    rows to the Microsoft refresher IN LIVE MODE and everything else (and all of
    mock, which must not touch the network) to the honest no-refresher fallback.

Microsoft ROTATES refresh tokens: the response's new refresh_token must replace the
stored one or the next refresh fails with a burnt token. That is why every success
path here re-puts the secret material before returning.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import live_mode
from app.connections import registry
from app.connections.secrets import SecretStore, get_secret_store
from app.models.connection import InstanceConnection

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"  # noqa: S105 — a public URL whose path ends in /token, not a credential  # nosec B105
# Refresh when the stored expiry is inside this window. Five minutes: long enough to
# cover clock skew and a slow Graph call, short enough that we are not refreshing on
# every poll.
REFRESH_LEAD = timedelta(minutes=5)


class _Refresher(Protocol):
    """Structural twin of the health worker's TokenRefresher (no import cycle)."""

    async def refresh(
        self, connection: InstanceConnection, store: SecretStore
    ) -> datetime | None: ...


class _PostTransport(Protocol):
    async def post(
        self, url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> Any: ...


def _transport_factory() -> Any:
    """Default transport, overridden in tests. Lazy httpx so mock paths never import-cost it."""
    import httpx  # noqa: PLC0415 — live-path-only import

    return httpx.AsyncClient(timeout=15.0)


async def _refresh_material(
    material: dict[str, str], *, transport: _PostTransport
) -> tuple[dict[str, str], datetime] | None:
    """One refresh-token grant against Microsoft; (new material, new expiry) or None.

    None on ANY failure — missing refresh token, unconfigured platform client, HTTP
    rejection — because every failure means the same thing to the caller: this
    credential cannot be made fresh, say so.
    """
    refresh_token = material.get("refresh_token", "")
    if not refresh_token:
        return None
    client = registry.oauth_client_config("outlook")
    if not client.configured:
        return None
    try:
        response = await transport.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                # Same scopes as the original consent; Microsoft rejects a scope-less
                # refresh for some account types rather than defaulting.
                "scope": "offline_access User.Read Mail.Read Mail.Send",
            },
        )
    except Exception as exc:  # noqa: BLE001 — a transport error is a failed refresh
        logger.warning("outlook_refresh_transport_error error=%s", type(exc).__name__)
        return None
    if getattr(response, "status_code", None) != 200:
        logger.warning("outlook_refresh_rejected status=%s", getattr(response, "status_code", "?"))
        return None
    payload = response.json()
    access_token = str(payload.get("access_token", ""))
    if not access_token:
        return None
    rotated = dict(material)
    rotated["access_token"] = access_token
    # Microsoft rotates refresh tokens; keep the old one only if no new one came back.
    if payload.get("refresh_token"):
        rotated["refresh_token"] = str(payload["refresh_token"])
    raw_expiry = str(payload.get("expires_in", "3600"))
    seconds = int(raw_expiry) if raw_expiry.isdigit() else 3600
    return rotated, datetime.now(UTC) + timedelta(seconds=seconds)


def _expiry_hint(connection: InstanceConnection) -> datetime | None:
    raw = (connection.non_secret_config or {}).get("token_expires_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _outlook_row(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> InstanceConnection | None:
    return (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "email_sender",
                InstanceConnection.provider == "outlook",
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()


async def ensure_fresh_token(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
    transport: _PostTransport | None = None,
    now: datetime | None = None,
) -> str | None:
    """An access token usable RIGHT NOW for this tenant's mailbox, or None.

    Refreshes when the stored expiry hint is inside :data:`REFRESH_LEAD` (or absent —
    an unknown expiry is treated as stale rather than immortal), persists the rotated
    material + new hint, and returns the fresh token. ``None`` means the mailbox needs
    reconnecting; the caller must refuse its operation honestly. This function never
    flips connection status itself — status transitions belong to the health worker
    and the send/poll callers, which know what the failure interrupted.
    """
    now = now or datetime.now(UTC)
    conn = await _outlook_row(session, admin_id, instance_id)
    if conn is None or not conn.secret_ref:
        return None
    backing = store or get_secret_store()
    material = await backing.get(conn.secret_ref) or {}
    access_token = material.get("access_token", "")
    if not access_token:
        return None

    expiry = _expiry_hint(conn)
    if expiry is not None and now < expiry - REFRESH_LEAD:
        return access_token

    refreshed = await _refresh_material(material, transport=transport or _transport_factory())
    if refreshed is None:
        return None
    rotated, new_expiry = refreshed
    # Rotation durable BEFORE the new token is used: Microsoft burns the old refresh
    # token at rotation, so an unpersisted rotation strands the row at the NEXT refresh.
    await backing.put(conn.secret_ref, rotated)
    cfg = dict(conn.non_secret_config or {})
    cfg["token_expires_at"] = new_expiry.isoformat()
    conn.non_secret_config = cfg
    return rotated["access_token"]


class MicrosoftTokenRefresher:
    """The health worker's proactive refresher for outlook rows (§3.8.5 steps 3–4).

    Implements the ``TokenRefresher`` protocol: new expiry on success (the worker
    persists the hint and stays connected), None on failure (the worker transitions
    to ``expired`` with an actionable reason). The rotated secret material is
    persisted HERE — the worker only knows about expiry hints, never token bytes.
    """

    def __init__(self, *, transport_factory: Any = None) -> None:
        self._transport_factory = transport_factory or _transport_factory

    async def refresh(self, connection: InstanceConnection, store: SecretStore) -> datetime | None:
        if not connection.secret_ref:
            return None
        material = await store.get(connection.secret_ref) or {}
        refreshed = await _refresh_material(material, transport=self._transport_factory())
        if refreshed is None:
            return None
        rotated, new_expiry = refreshed
        await store.put(connection.secret_ref, rotated)
        return new_expiry


class ProviderDispatchingRefresher:
    """Route each row to the refresher its provider actually has.

    ``outlook`` rows get the real Microsoft refresher — in LIVE mode only, because a
    refresh is a network call and mock must stay hermetic (in mock the fallback's
    honest None → ``expired`` is the truthful outcome: mock cannot refresh anything).
    Every other provider gets the fallback, which is today's behavior exactly.
    """

    def __init__(
        self, *, fallback: _Refresher, microsoft: MicrosoftTokenRefresher | None = None
    ) -> None:
        self._fallback = fallback
        self._microsoft = microsoft or MicrosoftTokenRefresher()

    async def refresh(self, connection: InstanceConnection, store: SecretStore) -> datetime | None:
        if connection.provider == "outlook" and live_mode():
            return await self._microsoft.refresh(connection, store)
        return await self._fallback.refresh(connection, store)
