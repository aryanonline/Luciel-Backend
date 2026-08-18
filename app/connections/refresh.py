"""Provider-agnostic OAuth token refresh — every provider, one mechanism (§3.8.5).

Before this module exactly ONE provider could actually be refreshed: Outlook
(``app/channels/tenant_outlook.py``). Google, HubSpot, Salesforce and Calendly
stored refresh tokens that nothing ever redeemed; Instagram's ~60-day token aged
out unnoticed; the health sweep's fallback refresher honestly reported "no
refresher exists" and expired the row. The owner-visible result was the reported
maintainability failure: a Google Calendar connection stopped working about an
hour after connect while its row still read Connected until the next sweep.

``REFRESH_SPECS`` declares, per provider, how its credential is made fresh:

  * ``refresh_token_post`` — the standard OAuth2 ``refresh_token`` grant
    (Google, HubSpot, Salesforce, Calendly, Outlook). Rotation-safe for every
    provider: a new ``refresh_token`` in the response replaces the stored one
    (Microsoft rotates; the others simply never return one).
  * ``ig_refresh_get``  — Instagram Login long-lived token self-refresh
    (``GET graph.instagram.com/refresh_access_token``; the token refreshes
    itself, no client secret involved).
  * ``fb_exchange_get`` — Meta long-lived token re-exchange
    (``GET graph.facebook.com/oauth/access_token`` with ``fb_exchange_token``).

Adding a provider is one spec line; the refresher itself is generic. Providers
with no spec (e.g. Notion, whose tokens do not expire) keep the fallback path.

The rules inherited from the Outlook implementation, applied uniformly:
  * rotated material is persisted to the SecretStore BEFORE the new token is
    used or an expiry is returned — an unpersisted rotation strands the row at
    the next refresh for rotating providers;
  * ``None`` on ANY failure, because every failure means the same thing to the
    caller: this credential cannot be made fresh, say so honestly;
  * live mode only — mock never touches the network. The health sweep's
    fallback (``health.ReconnectOnExpiry``) keeps owning mock and unspecced
    providers with its honest None → ``expired`` transition.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.connections.health import TokenRefresher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import live_mode
from app.connections import registry
from app.connections.secrets import SecretStore, get_secret_store
from app.models.connection import InstanceConnection

logger = logging.getLogger(__name__)

# Refresh proactively when inside this window of the stored expiry hint (same
# rationale as tenant_outlook.REFRESH_LEAD: covers clock skew and a slow
# provider call without refreshing on every use).
REFRESH_LEAD = timedelta(minutes=5)

STYLE_REFRESH_TOKEN_POST = "refresh_token_post"  # noqa: S105 — a grant-style name, not a secret
STYLE_IG_REFRESH_GET = "ig_refresh_get"
STYLE_FB_EXCHANGE_GET = "fb_exchange_get"


@dataclass(frozen=True)
class RefreshSpec:
    """How one provider's stored credential is made fresh."""

    style: str
    # Token endpoint for refresh_token_post; the fixed refresh/exchange URL for
    # the GET styles.
    endpoint: str
    # The registry provider id whose OAuth client credentials sign the request
    # (usually the row's own provider; kept explicit so shared clients — the one
    # Google app serving Calendar AND Drive — stay obvious).
    client_provider: str | None = None
    # Some providers reject a scope-less refresh rather than defaulting
    # (Microsoft); most must NOT receive one.
    scope: str | None = None
    # Fallback lifetime when the provider's response omits expires_in
    # (Salesforce frequently does): refresh again on this cadence rather than
    # treating the token as immortal.
    default_lifetime_seconds: int = 3600


REFRESH_SPECS: dict[str, RefreshSpec] = {
    # One Google OAuth client serves Calendar + Drive; both row providers
    # refresh at the same endpoint with the same client.
    "google_calendar": RefreshSpec(
        style=STYLE_REFRESH_TOKEN_POST,
        endpoint="https://oauth2.googleapis.com/token",
        client_provider="google_calendar",
    ),
    "google_drive": RefreshSpec(
        style=STYLE_REFRESH_TOKEN_POST,
        endpoint="https://oauth2.googleapis.com/token",
        client_provider="google_drive",
    ),
    "hubspot": RefreshSpec(
        style=STYLE_REFRESH_TOKEN_POST,
        endpoint="https://api.hubapi.com/oauth/v1/token",
    ),
    "salesforce": RefreshSpec(
        style=STYLE_REFRESH_TOKEN_POST,
        endpoint="https://login.salesforce.com/services/oauth2/token",
        # Salesforce token responses carry no reliable expires_in; 2h keeps the
        # sweep probing/refreshing it rather than assuming immortality.
        default_lifetime_seconds=7200,
    ),
    "calendly": RefreshSpec(
        style=STYLE_REFRESH_TOKEN_POST,
        endpoint="https://auth.calendly.com/oauth/token",
    ),
    # Outlook joins the generic table (tenant_outlook.py keeps its own
    # send/poll-path helper; the SWEEP path routes here). Microsoft requires
    # the scopes re-sent and rotates refresh tokens — both handled generically.
    "outlook": RefreshSpec(
        style=STYLE_REFRESH_TOKEN_POST,
        endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scope="offline_access User.Read Mail.Read Mail.Send",
    ),
    # Instagram Login long-lived token: refreshes ITSELF via GET; ~60-day life.
    "instagram": RefreshSpec(
        style=STYLE_IG_REFRESH_GET,
        endpoint="https://graph.instagram.com/refresh_access_token",
        default_lifetime_seconds=60 * 24 * 3600,
    ),
    # Meta (WhatsApp + Messenger) long-lived token: re-exchange with the app
    # credentials; ~60-day life.
    "meta": RefreshSpec(
        style=STYLE_FB_EXCHANGE_GET,
        endpoint="https://graph.facebook.com/v19.0/oauth/access_token",
        default_lifetime_seconds=60 * 24 * 3600,
    ),
}


class _Transport(Protocol):
    async def post(
        self, url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> Any: ...

    async def get(self, url: str, *, params: dict[str, str] | None = None) -> Any: ...


def _transport_factory() -> Any:
    """Default transport, overridden in tests. Lazy httpx so mock paths never import-cost it."""
    import httpx  # noqa: PLC0415 — live-path-only import

    return httpx.AsyncClient(timeout=15.0)


def _payload_of(response: Any) -> dict[str, Any] | None:
    if getattr(response, "status_code", None) != 200:
        logger.warning("token_refresh_rejected status=%s", getattr(response, "status_code", "?"))
        return None
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def _lifetime(payload: dict[str, Any], spec: RefreshSpec) -> timedelta:
    raw = str(payload.get("expires_in", "")).strip()
    seconds = int(raw) if raw.isdigit() else spec.default_lifetime_seconds
    return timedelta(seconds=seconds)


async def _refresh_material(
    provider: str,
    material: dict[str, str],
    *,
    spec: RefreshSpec,
    transport: _Transport,
) -> tuple[dict[str, str], datetime] | None:
    """One refresh call for ``provider``; (rotated material, new expiry) or None.

    None on ANY failure — missing token, unconfigured platform client, transport
    error, provider rejection — because every failure means the same thing to
    the caller: this credential cannot be made fresh.
    """
    try:
        if spec.style == STYLE_REFRESH_TOKEN_POST:
            refresh_token = material.get("refresh_token", "")
            if not refresh_token:
                return None
            client = registry.oauth_client_config(spec.client_provider or provider)
            if not client.configured:
                return None
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
            }
            if spec.scope:
                data["scope"] = spec.scope
            payload = _payload_of(await transport.post(spec.endpoint, data=data))
        elif spec.style == STYLE_IG_REFRESH_GET:
            token = material.get("access_token", "")
            if not token:
                return None
            payload = _payload_of(
                await transport.get(
                    spec.endpoint,
                    params={"grant_type": "ig_refresh_token", "access_token": token},
                )
            )
        elif spec.style == STYLE_FB_EXCHANGE_GET:
            token = material.get("access_token", "")
            if not token:
                return None
            client = registry.oauth_client_config(spec.client_provider or provider)
            if not client.configured:
                return None
            payload = _payload_of(
                await transport.get(
                    spec.endpoint,
                    params={
                        "grant_type": "fb_exchange_token",
                        "client_id": client.client_id,
                        "client_secret": client.client_secret,
                        "fb_exchange_token": token,
                    },
                )
            )
        else:  # pragma: no cover — a spec typo is a programming error, fail safe
            return None
    except Exception as exc:  # noqa: BLE001 — a transport error is a failed refresh
        logger.warning(
            "token_refresh_transport_error provider=%s error=%s", provider, type(exc).__name__
        )
        return None

    if payload is None:
        return None
    access_token = str(payload.get("access_token", ""))
    if not access_token:
        return None
    rotated = dict(material)
    rotated["access_token"] = access_token
    # Rotation-safe for every provider: keep the old refresh token only when no
    # new one came back (Microsoft rotates; Google/HubSpot/Salesforce don't).
    if payload.get("refresh_token"):
        rotated["refresh_token"] = str(payload["refresh_token"])
    return rotated, datetime.now(UTC) + _lifetime(payload, spec)


class RegistryTokenRefresher:
    """The health worker's proactive refresher for every specced provider.

    Implements the ``TokenRefresher`` protocol (app/connections/health.py): new
    expiry on success (the worker persists the hint and stays connected), None
    on failure (the worker transitions to ``expired`` with an actionable
    reason). Rotated secret material is persisted HERE, before returning — the
    worker only ever sees expiry hints, never token bytes.
    """

    def __init__(self, *, transport_factory: Any = None) -> None:
        self._transport_factory = transport_factory or _transport_factory

    async def refresh(self, connection: InstanceConnection, store: SecretStore) -> datetime | None:
        spec = REFRESH_SPECS.get(connection.provider)
        if spec is None or not connection.secret_ref:
            return None
        material = await store.get(connection.secret_ref) or {}
        refreshed = await _refresh_material(
            connection.provider, material, spec=spec, transport=self._transport_factory()
        )
        if refreshed is None:
            return None
        rotated, new_expiry = refreshed
        await store.put(connection.secret_ref, rotated)
        return new_expiry


class DispatchingRefresher:
    """What the scheduled sweep binds: route each row to the refresher it has.

    A provider with a ``REFRESH_SPECS`` entry gets the real refresher — in LIVE
    mode only, because a refresh is a network call and mock must stay hermetic
    (in mock the fallback's honest None → ``expired`` is the truthful outcome).
    Everything else gets the fallback, which is the pre-existing behavior
    exactly. Supersedes ``tenant_outlook.ProviderDispatchingRefresher`` (which
    routed only ``outlook``); Outlook now rides the same generic table.
    """

    def __init__(
        self, *, fallback: TokenRefresher, registry_refresher: RegistryTokenRefresher | None = None
    ):
        self._fallback = fallback
        self._registry = registry_refresher or RegistryTokenRefresher()

    async def refresh(self, connection: InstanceConnection, store: SecretStore) -> datetime | None:
        if connection.provider in REFRESH_SPECS and live_mode():
            return await self._registry.refresh(connection, store)
        return await self._fallback.refresh(connection, store)


def _expiry_hint(connection: InstanceConnection) -> datetime | None:
    raw = (connection.non_secret_config or {}).get("token_expires_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def ensure_fresh_access_token(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    connection_type: str,
    store: SecretStore | None = None,
    transport: _Transport | None = None,
    now: datetime | None = None,
) -> str | None:
    """An access token usable RIGHT NOW for the tenant's ``connection_type`` row.

    The generic twin of ``tenant_outlook.ensure_fresh_token``, for the tool
    call-time path (calendar booking, CRM push): refreshes when the stored
    expiry hint is inside :data:`REFRESH_LEAD` — or absent, since an unknown
    expiry is stale rather than immortal — persists the rotation + new hint,
    and returns the fresh token. In mock, or for a provider with no spec, the
    stored token is returned untouched (there is nothing hermetic or possible
    to refresh). ``None`` means the connection needs reconnecting; the caller
    must refuse its operation honestly. Never flips connection status — that
    belongs to the health worker and the callers, which know what the failure
    interrupted.
    """
    now = now or datetime.now(UTC)
    conn = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == connection_type,
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        return None
    # The connect flow stamps secret_ref; rows that predate it (and fixtures)
    # rely on the vm/{admin}/{instance}/{type} convention the store has always
    # used — the same fallback the direct-read paths applied before this helper.
    from app.connections.secrets import secret_ref_for  # noqa: PLC0415

    secret_ref = conn.secret_ref or secret_ref_for(admin_id, instance_id, connection_type)
    backing = store or get_secret_store()
    material = await backing.get(secret_ref) or {}
    access_token = material.get("access_token", "")
    if not access_token:
        return None

    spec = REFRESH_SPECS.get(conn.provider)
    if spec is None or not live_mode():
        return access_token

    expiry = _expiry_hint(conn)
    if expiry is not None and now < expiry - REFRESH_LEAD:
        return access_token

    refreshed = await _refresh_material(
        conn.provider, material, spec=spec, transport=transport or _transport_factory()
    )
    if refreshed is None:
        return None
    rotated, new_expiry = refreshed
    # Rotation durable BEFORE the new token is used (the Outlook rule, §3.8.5).
    await backing.put(secret_ref, rotated)
    cfg = dict(conn.non_secret_config or {})
    cfg["token_expires_at"] = new_expiry.isoformat()
    conn.non_secret_config = cfg
    return rotated["access_token"]
