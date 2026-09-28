"""Provider liveness probes for the health sweep (round 6 WP-C, §3.8.5).

``health.StoredCredentialProbe`` answers one question authoritatively: do we still
hold a credential? It deliberately never asked the provider, so a grant the owner
revoked at the provider (or that expired with no refresh) read Connected until a
customer's booking or a lead push failed in front of them. This module adds the
second question — does the provider still accept it? — for the LIVE posture only,
with one cheap identity call per connection per sweep and an honest boundary:

  * 2xx → healthy;
  * 401 / 403 → one refresh attempt where the provider supports it, then a re-probe;
    still refused → ``error`` with "rejected the stored credential — reconnect";
  * anything else (5xx, 429, timeouts, a 404 from an endpoint that moved) →
    ``probe_skipped`` — an outage at the provider is not evidence the grant is dead,
    and the sweep must never flip every tenant to ``error`` because a provider blinked.

Mock posture and providers with no identity call keep the stored-credential answer
exactly (``DispatchingProbe``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from app.config import live_mode
from app.connections import registry
from app.connections.health import ConnectionProbe, ProbeResult, TokenRefresher
from app.connections.secrets import SecretStore, get_secret_store
from app.connections.verify import (
    CALENDLY_ME_ENDPOINT,
    GOOGLE_TOKENINFO_ENDPOINT,
    GRAPH_ME_ENDPOINT,
    HUBSPOT_TOKEN_INFO_ENDPOINT,
    INSTAGRAM_ME_ENDPOINT,
    META_ME_ENDPOINT,
    NOTION_ME_ENDPOINT,
    NOTION_VERSION,
    TWILIO_ACCOUNTS_ENDPOINT,
)
from app.models.connection import InstanceConnection

logger = logging.getLogger(__name__)

LIVENESS_TIMEOUT_SECONDS = 10.0
TWILIO_ACCOUNT_ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts/{sid}.json"
#: Statuses that mean "the provider answered and refused THIS credential".
AUTH_REFUSED_STATUSES = frozenset({401, 403})
#: The synthetic lookup key the record-source live connector is asked for — at
#: connect time and by the sweep. Never a real customer query: the proof is that the
#: endpoint is reachable and accepts the key, not that any record exists.
LIVE_CONNECTOR_PROBE_QUERY = "__luciel_probe__"


class LiveConnectorProofFailed(Exception):
    """The owner's record endpoint did not prove itself. Carries an owner-facing reason."""


async def prove_live_connector(*, url: str, api_key: str | None, transport: Any = None) -> None:
    """Connect-time proof for the ``live_connector`` record source (round 7 WP-4a item 1).

    One authenticated ``GET <url>?q=<synthetic key>`` through the record transport
    (``build_record_transport``: the pinned httpx call in live, the seeded mock
    otherwise). The honest boundary:

      * 2xx → the endpoint must answer a record array (or ``{"records": [...]}``) —
        HTML or a bare object is not a records endpoint;
      * 401 / 403 → the endpoint refused the API key: the connect fails;
      * any other 4xx → reachable and the key was accepted; the synthetic key simply
        matches nothing (a 404 for an unknown record is a correct answer);
      * 5xx, a timeout, a transport error → the endpoint did not answer: the connect
        fails honestly rather than minting a Connected row over a dead URL.

    Before this the proof required ``status < 400`` AND a record array, and ran only
    for a provider id (``api``) the connect flow never wrote — so it never ran.
    """
    from app.tools.record_backend import (  # noqa: PLC0415 — keeps the probe import light
        RecordSourceUnavailable,
        build_record_transport,
        parse_record_response,
    )

    transport = transport if transport is not None else build_record_transport()
    separator = "&" if "?" in url else "?"
    target = f"{url}{separator}q={quote(LIVE_CONNECTOR_PROBE_QUERY, safe='')}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        status, body = await transport.get(target, headers=headers)
    except RecordSourceUnavailable as exc:
        raise LiveConnectorProofFailed(
            f"Your record lookup URL could not be read ({exc}). Check the address."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — transport detail must not leak
        raise LiveConnectorProofFailed(
            "Your record lookup URL did not answer a test query "
            f"({type(exc).__name__}). Check the address."
        ) from exc
    if status in AUTH_REFUSED_STATUSES:
        raise LiveConnectorProofFailed(
            f"Your record lookup URL refused the API key (HTTP {status}). Check the key."
        )
    if 200 <= status < 300:
        try:
            parse_record_response(body)
        except RecordSourceUnavailable as exc:
            raise LiveConnectorProofFailed(
                "Your record lookup URL answered a test query with something other than a "
                f"JSON array of records ({exc}). Check the address."
            ) from exc
        return
    if 400 <= status < 500:
        return  # reachable, key accepted, no such record — an honest "not found"
    raise LiveConnectorProofFailed(
        f"Your record lookup URL did not answer a test query (HTTP {status}). "
        "Check that the endpoint is up and try again."
    )


class WebhookProofFailed(Exception):
    """The owner's webhook endpoint did not prove itself. Carries an owner-facing reason."""


async def prove_webhook_endpoint(
    *, url: str, auth_header: str | None, transport: Any = None
) -> None:
    """Connect-time proof for the URL-shaped providers — ``custom_webhook`` and
    ``outbound_webhook`` (round 7 WP-4a item 4).

    One ``GET <url>`` carrying the owner's Authorization header, through the same
    pinned transport the record proof uses (``build_record_transport``: connect to the
    validated address with Host/SNI in live, the seeded mock otherwise). A webhook
    receiver is a POST target, so the proof asks only what a GET can honestly answer:

      * 401 / 403 → the endpoint refused the Authorization header: the connect fails;
      * 5xx, a timeout, a transport error → the endpoint did not answer: the connect
        fails rather than minting a Connected row over a dead URL;
      * anything else (2xx, a 404 or 405 from a POST-only receiver) → reachable, and
        the header was accepted.

    Before this a credential-form swap onto a webhook provider wrote the new material
    straight over the live secret and flipped the row to connected with no proof at
    all — a HubSpot tenant moving to an endpoint that refused their header lost the
    working HubSpot credential and gained a dead webhook.
    """
    from app.tools.record_backend import (  # noqa: PLC0415 — keeps the probe import light
        RecordSourceUnavailable,
        build_record_transport,
    )

    transport = transport if transport is not None else build_record_transport()
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    try:
        status, _body = await transport.get(url, headers=headers)
    except RecordSourceUnavailable as exc:
        raise WebhookProofFailed(
            f"Your webhook URL could not be reached ({exc}). Check the address."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — transport detail must not leak
        raise WebhookProofFailed(
            f"Your webhook URL did not answer a test request ({type(exc).__name__}). "
            "Check the address."
        ) from exc
    if status in AUTH_REFUSED_STATUSES:
        raise WebhookProofFailed(
            f"Your webhook URL refused the Authorization header (HTTP {status}). "
            "Check the header value."
        )
    if status >= 500:
        raise WebhookProofFailed(
            f"Your webhook URL did not answer a test request (HTTP {status}). "
            "Check that the endpoint is up and try again."
        )


@dataclass(frozen=True)
class LivenessRequest:
    """The one GET that proves a stored credential still works at its provider."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    # HTTP Basic credentials (Twilio API-key / auth-token tenants).
    auth: tuple[str, str] | None = None


def liveness_request_for(
    connection: InstanceConnection, material: dict[str, str]
) -> LivenessRequest | None:
    """The identity call for this row, or None when the provider has none we can make
    (a provisioned sender, a bare webhook URL, a website crawl)."""
    provider = registry.canonical_provider(connection.provider)
    cfg = connection.non_secret_config or {}
    token = str(material.get("access_token") or "").strip()
    bearer = {"Authorization": f"Bearer {token}"}
    if provider == "salesforce":
        base = str(material.get("instance_url") or cfg.get("instanceUrl") or "").rstrip("/")
        if not token or not base:
            return None
        return LivenessRequest(f"{base}/services/oauth2/userinfo", bearer)
    if provider == "hubspot":
        return (
            LivenessRequest(f"{HUBSPOT_TOKEN_INFO_ENDPOINT}/{quote(token, safe='')}")
            if token
            else None
        )
    if provider in ("google_calendar", "google_drive"):
        return (
            LivenessRequest(f"{GOOGLE_TOKENINFO_ENDPOINT}?access_token={quote(token, safe='')}")
            if token
            else None
        )
    if provider == "notion":
        return (
            LivenessRequest(NOTION_ME_ENDPOINT, {**bearer, "Notion-Version": NOTION_VERSION})
            if token
            else None
        )
    if provider == "calendly":
        return LivenessRequest(CALENDLY_ME_ENDPOINT, bearer) if token else None
    if provider == "meta":
        return LivenessRequest(f"{META_ME_ENDPOINT}?fields=id", bearer) if token else None
    if provider == "instagram":
        return LivenessRequest(f"{INSTAGRAM_ME_ENDPOINT}?fields=id", bearer) if token else None
    if provider == "outlook":
        return LivenessRequest(GRAPH_ME_ENDPOINT, bearer) if token else None
    if provider == "twilio":
        if connection.auth_class == "oauth_token" and token:
            return LivenessRequest(TWILIO_ACCOUNTS_ENDPOINT, bearer)
        sid = str(cfg.get("accountSid") or "").strip()
        key_sid = str(cfg.get("apiKeySid") or material.get("apiKeySid") or "").strip()
        key_secret = str(material.get("apiKeySecret") or "").strip()
        auth_token = str(material.get("authToken") or "").strip()
        if sid and key_sid and key_secret:
            return LivenessRequest(
                TWILIO_ACCOUNT_ENDPOINT.format(sid=sid), auth=(key_sid, key_secret)
            )
        if sid and auth_token:
            return LivenessRequest(TWILIO_ACCOUNT_ENDPOINT.format(sid=sid), auth=(sid, auth_token))
        return None
    return None


def _default_transport_factory() -> Any:
    import httpx  # noqa: PLC0415 — the live path only

    return httpx.AsyncClient(timeout=LIVENESS_TIMEOUT_SECONDS)


class ProviderLivenessProbe:
    """Ask the provider whether the stored credential still works (live posture)."""

    def __init__(
        self,
        *,
        store: SecretStore | None = None,
        transport_factory: Any = None,
        refresher: TokenRefresher | None = None,
    ) -> None:
        self._store = store
        self._transport_factory = transport_factory or _default_transport_factory
        self._refresher = refresher

    def _refresher_for(self) -> TokenRefresher:
        if self._refresher is not None:
            return self._refresher
        from app.connections.refresh import RegistryTokenRefresher  # noqa: PLC0415

        return RegistryTokenRefresher()

    async def _status_of(self, request: LivenessRequest) -> int | None:
        try:
            async with self._transport_factory() as client:
                response = await client.get(request.url, headers=request.headers, auth=request.auth)
        except Exception as exc:  # noqa: BLE001 — a transport failure is "could not ask"
            logger.info("liveness_probe_transport_error error=%s", type(exc).__name__)
            return None
        status = getattr(response, "status_code", None)
        return status if isinstance(status, int) else None

    async def probe(self, connection: InstanceConnection) -> ProbeResult:
        store = self._store or get_secret_store()
        material: dict[str, str] = {}
        if connection.secret_ref:
            try:
                material = await store.get(connection.secret_ref) or {}
            except Exception as exc:  # noqa: BLE001 — a store outage is not a dead grant
                return ProbeResult(ok=True, detail=f"probe_skipped:{type(exc).__name__}")
        request = liveness_request_for(connection, material)
        if request is None:
            return ProbeResult(ok=True, detail="probe_skipped:no_liveness_probe")
        status = await self._status_of(request)
        if status in AUTH_REFUSED_STATUSES:
            # The token may merely have aged out between sweeps: one refresh, one
            # re-probe, before the row is declared dead.
            new_expiry = await self._refresher_for().refresh(connection, store)
            if new_expiry is not None:
                cfg = dict(connection.non_secret_config or {})
                cfg["token_expires_at"] = new_expiry.isoformat()
                connection.non_secret_config = cfg
                try:
                    material = await store.get(connection.secret_ref or "") or {}
                except Exception:  # noqa: BLE001
                    material = {}
                retried = liveness_request_for(connection, material)
                status = await self._status_of(retried) if retried else None
                if status is not None and 200 <= status < 300:
                    return ProbeResult(ok=True)
            spec = registry.get_provider(connection.provider)
            name = spec.display_name if spec is not None else connection.provider
            return ProbeResult(
                ok=False,
                detail=f"{name} rejected the stored credential (HTTP {status}) — reconnect",
            )
        if status is None:
            return ProbeResult(ok=True, detail="probe_skipped:transport_error")
        if 200 <= status < 300:
            return ProbeResult(ok=True)
        # 5xx, 429, a moved endpoint: the provider did not say the grant is dead.
        return ProbeResult(ok=True, detail=f"probe_skipped:http_{status}")


class DispatchingProbe:
    """What the scheduled sweep binds: the stored-credential check for every row, then
    the provider liveness call in live mode for providers that have one."""

    def __init__(
        self, *, fallback: ConnectionProbe, liveness: ProviderLivenessProbe | None = None
    ) -> None:
        self._fallback = fallback
        self._liveness = liveness or ProviderLivenessProbe()

    async def probe(self, connection: InstanceConnection) -> ProbeResult:
        base = await self._fallback.probe(connection)
        if not base.ok or not live_mode():
            return base
        return await self._liveness.probe(connection)
