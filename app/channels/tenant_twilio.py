"""The TENANT's own Twilio account — BYO telephony (Architecture §3.1.4).

§3.1.4 is explicit that VantageMind is never in the telephony billing path: "carrier
costs are the Admin's, paid to their own carrier". That can only be true if the
credential on the send path belongs to the customer, so SMS/voice runs on the Twilio
account the admin connected as their ``sms_sender`` connection — exactly the same shape
as connecting their own HubSpot, and the same shape as a business equipping a new hire
with the business's own phone line.

Credential material lives in the SecretStore under the connection's ``secret_ref``
(§3.8.3, never in Postgres); the Account SID and the designated from-number are
non-secret operational config on the row. Twilio accepts two credential shapes and a
customer may hand us either: Account SID + Auth Token, or an API Key SID/secret
*alongside* the Account SID (an ``SK...`` key authenticates against an account, it does
not replace it).

The platform's own Twilio survives only as the dev/mock fallback in
``twilio_sender.build_sms_sender`` — never as a customer's sender.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.twilio_sender import NullSmsSender, SmsSenderProtocol, TwilioSmsSender
from app.connections.secrets import SecretStore, get_secret_store
from app.models.connection import InstanceConnection

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.channels.number_operability import NumberOperabilityProbe

# Field names on the connect form (registry ``twilio`` credential_fields) and, for the
# secret half, the keys the material is stored under.
FIELD_ACCOUNT_SID = "accountSid"
FIELD_AUTH_TOKEN = "authToken"  # noqa: S105 — field NAME, not a credential  # nosec B105
FIELD_API_KEY_SID = "apiKeySid"
FIELD_API_KEY_SECRET = "apiKeySecret"  # noqa: S105 — field NAME, not a credential  # nosec B105
# Per-connection random secret appended as ?cap= to auto-configured webhook URLs
# (C11): the inbound-authenticity proof for tenants whose Account Auth Token the
# platform does not hold (OAuth grants, API-key-only submissions).
FIELD_CAPABILITY_TOKEN = "webhookCapabilityToken"  # noqa: S105 — field NAME  # nosec B105


class InvalidTwilioCredentials(ValueError):
    """The submitted Twilio credential cannot authenticate a Twilio REST call."""


@dataclass(frozen=True)
class TwilioCredentials:
    """One tenant's Twilio credential, reassembled from row + SecretStore.

    Two shapes since C10: the credential-form pair (auth token / API key) and the
    one-click OAuth grant (``access_token`` — a bearer token the ``twilio_bearer``
    surface uses; possibly stale, so LIVE calls go through
    ``refresh.ensure_fresh_access_token`` rather than reading it directly)."""

    account_sid: str
    auth_token: str = ""
    api_key_sid: str = ""
    api_key_secret: str = ""
    access_token: str = ""

    @property
    def usable(self) -> bool:
        """True iff this can authenticate the SDK's credential-shape client."""
        return bool(self.account_sid) and bool(
            self.auth_token or (self.api_key_sid and self.api_key_secret)
        )

    @property
    def bearer_usable(self) -> bool:
        """True iff this is the OAuth shape (bearer REST calls, C10)."""
        return bool(self.account_sid) and bool(self.access_token)


def validate_submitted_credentials(fields: dict[str, str]) -> TwilioCredentials:
    """Shape-check a connect-form submission before anything is stored.

    Rejecting here rather than at first send is what keeps the connection status honest:
    a row that says ``connected`` over a credential Twilio would refuse is the same
    silent-failure the BYO-number operability gate exists to prevent."""
    account_sid = (fields.get(FIELD_ACCOUNT_SID) or "").strip()
    creds = TwilioCredentials(
        account_sid=account_sid,
        auth_token=(fields.get(FIELD_AUTH_TOKEN) or "").strip(),
        api_key_sid=(fields.get(FIELD_API_KEY_SID) or "").strip(),
        api_key_secret=(fields.get(FIELD_API_KEY_SECRET) or "").strip(),
    )
    if not account_sid.startswith("AC"):
        raise InvalidTwilioCredentials(
            "Twilio Account SID must start with 'AC'. An API Key SID (SK…) is not an "
            "Account SID — Twilio authenticates API keys against an account."
        )
    if not creds.usable:
        raise InvalidTwilioCredentials(
            "Enter your Twilio Auth Token, or an API Key SID and its secret."
        )
    return creds


def secret_material(creds: TwilioCredentials) -> dict[str, str]:
    """The half of the credential that goes to the SecretStore (§3.8.3).

    The Account SID is an identifier, not a secret, and stays on the row so the send
    path can name the account without unsealing anything."""
    material = {}
    if creds.auth_token:
        material[FIELD_AUTH_TOKEN] = creds.auth_token
    if creds.api_key_sid:
        material[FIELD_API_KEY_SID] = creds.api_key_sid
    if creds.api_key_secret:
        material[FIELD_API_KEY_SECRET] = creds.api_key_secret
    return material


async def verify_credentials(creds: TwilioCredentials) -> None:
    """Prove the credential works, live. No-op outside live mode (hermetic CI).

    The mock-mode skip is DELIBERATE and disclosed: outside live mode a submit is
    accepted without any provider check (nothing to verify against), so a mock
    "connected" status is a wiring statement, never a claim the credential was
    proven. Live mode is the only place verification is asserted.

    A lightweight account fetch is the cheapest call that exercises exactly the auth
    path a send will use. Raises :class:`InvalidTwilioCredentials` on refusal."""
    from app.config import get_settings

    if get_settings().integration_mode != "live":
        return
    try:
        import asyncio

        from twilio.rest import Client  # noqa: PLC0415 — lazy/guarded (CI has no network)

        client = (
            Client(creds.api_key_sid, creds.api_key_secret, creds.account_sid)
            if creds.api_key_sid
            else Client(creds.account_sid, creds.auth_token)
        )
        await asyncio.to_thread(lambda: client.api.accounts(creds.account_sid).fetch())
    except ImportError:  # pragma: no cover - the SDK is a live-mode dependency
        return
    except Exception as exc:  # noqa: BLE001 — any refusal is the same answer to the admin
        raise InvalidTwilioCredentials(
            "Twilio rejected these credentials. Check the Account SID and token/key."
        ) from exc


async def _sms_sender_row(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> InstanceConnection | None:
    return (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "sms_sender",
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()


async def load_tenant_credentials(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> TwilioCredentials | None:
    """The tenant's own Twilio credential, or None if they have not connected one."""
    conn = await _sms_sender_row(session, admin_id, instance_id)
    if conn is None or not conn.secret_ref:
        return None
    account_sid = (conn.non_secret_config or {}).get(FIELD_ACCOUNT_SID)
    if not isinstance(account_sid, str) or not account_sid:
        return None
    material = await (store or get_secret_store()).get(conn.secret_ref) or {}
    return TwilioCredentials(
        account_sid=account_sid,
        auth_token=material.get(FIELD_AUTH_TOKEN, ""),
        api_key_sid=material.get(FIELD_API_KEY_SID, ""),
        api_key_secret=material.get(FIELD_API_KEY_SECRET, ""),
        access_token=material.get("access_token", ""),
    )


async def _fresh_bearer_token(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> str | None:
    """A refresh-checked bearer token for the tenant's sms_sender row (C10).

    OAuth access tokens are short-lived; a factory building a live surface must
    not hand out a token that expires mid-use. ``None`` means reconnect needed —
    every caller degrades to its honest Null."""
    from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

    return await ensure_fresh_access_token(
        session, admin_id, instance_id, connection_type="sms_sender", store=store
    )


async def tenant_auth_token(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> str | None:
    """The tenant's Twilio Auth Token, for X-Twilio-Signature verification on inbound.

    Inbound webhooks are signed with the ACCOUNT auth token of whichever account owns the
    receiving number — under BYO that is the tenant's account, so verifying against the
    platform token would reject every real inbound."""
    creds = await load_tenant_credentials(session, admin_id, instance_id, store=store)
    return creds.auth_token if creds is not None and creds.auth_token else None


async def tenant_inbound_verification(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> tuple[str | None, str | None]:
    """Both inbound-authenticity secrets for this tenant: (auth_token, capability_token).

    The signature path verifies with the Account Auth Token when the tenant gave
    us one; the capability-URL path (C11) covers the shapes that never yield it
    (OAuth grant, API-key-only). Either may be None; both None means only the
    platform-token fallback remains — which for a BYO number honestly fails."""
    conn = await _sms_sender_row(session, admin_id, instance_id)
    if conn is None or not conn.secret_ref:
        return None, None
    material = await (store or get_secret_store()).get(conn.secret_ref) or {}
    return (
        material.get(FIELD_AUTH_TOKEN) or None,
        material.get(FIELD_CAPABILITY_TOKEN) or None,
    )


async def ensure_capability_token(
    conn: InstanceConnection,
    *,
    store: SecretStore | None = None,
) -> tuple[str | None, bool]:
    """The connection's webhook capability token, minting one if absent (C11).

    Returns ``(token, minted)``. Only for connections whose material lacks an
    Account Auth Token — a tenant the signature path already covers gets
    ``(None, False)``, so their verification stays byte-identical. Also None
    when there is no secret material at all (nothing to anchor a webhook to).
    The token is 256-bit urlsafe random, stored beside the credential in the
    SecretStore, never on the row."""
    import secrets as pysecrets  # noqa: PLC0415

    if not conn.secret_ref:
        return None, False
    backing = store or get_secret_store()
    material = await backing.get(conn.secret_ref)
    if material is None:
        return None, False
    if material.get(FIELD_AUTH_TOKEN):
        return None, False
    existing = material.get(FIELD_CAPABILITY_TOKEN)
    if existing:
        return str(existing), False
    token = pysecrets.token_urlsafe(32)
    material[FIELD_CAPABILITY_TOKEN] = token
    await backing.put(conn.secret_ref, material)
    return token, True


async def build_tenant_operability_probe(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> NumberOperabilityProbe:
    """Probe number operability against the TENANT's Twilio account (§3.1.4).

    "BYO" means the number must be operable by *their* account. Probing the platform's
    account would answer a question nobody asked and would mark every customer number
    non-operable."""
    from app.channels.number_operability import NullOperabilityProbe, TwilioOperabilityProbe
    from app.config import get_settings

    if get_settings().integration_mode != "live":
        return NullOperabilityProbe()
    creds = await load_tenant_credentials(session, admin_id, instance_id, store=store)
    if creds is None:
        return NullOperabilityProbe()
    if creds.usable:
        return TwilioOperabilityProbe(
            account_sid=creds.account_sid,
            api_key_sid=creds.api_key_sid,
            api_key_secret=creds.api_key_secret,
            auth_token=creds.auth_token,
        )
    if creds.bearer_usable:
        from app.channels.twilio_bearer import TwilioBearerOperabilityProbe  # noqa: PLC0415

        token = await _fresh_bearer_token(session, admin_id, instance_id, store=store)
        if token:
            return TwilioBearerOperabilityProbe(account_sid=creds.account_sid, access_token=token)
    return NullOperabilityProbe()


@dataclass(frozen=True)
class TenantNumber:
    """One number in the tenant's own Twilio account (the designate picker, C9)."""

    phone_number: str  # E.164
    friendly_name: str


# Deterministic mock-mode picker fixture: the Twilio magic test number + a second
# line, so the picker UX is exercisable end-to-end without any network.
MOCK_TENANT_NUMBERS: tuple[TenantNumber, ...] = (
    TenantNumber(phone_number="+15005550006", friendly_name="Test line"),
    TenantNumber(phone_number="+15005550007", friendly_name="Second test line"),
)


async def list_tenant_numbers(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
    limit: int = 50,
) -> list[TenantNumber] | None:
    """The numbers in the TENANT's Twilio account, for the designate picker (C9).

    The platform already holds credentials able to list them (it uses them for the
    webhook auto-config), yet the owner was typing their E.164 by hand — the picker
    closes that. Returns None when listing is not possible (no usable credential,
    or the provider refused): the caller falls back to manual entry, honestly,
    rather than blocking the designate step on this convenience."""
    from app.config import get_settings

    creds = await load_tenant_credentials(session, admin_id, instance_id, store=store)
    if creds is None or not (creds.usable or creds.bearer_usable):
        return None
    if get_settings().integration_mode != "live":
        return list(MOCK_TENANT_NUMBERS)
    if not creds.usable:
        # OAuth shape (C10): the same listing over bearer auth.
        from app.channels.twilio_bearer import list_incoming_numbers  # noqa: PLC0415

        token = await _fresh_bearer_token(session, admin_id, instance_id, store=store)
        if not token:
            return None
        bearer_rows = await list_incoming_numbers(
            access_token=token, account_sid=creds.account_sid, limit=limit
        )
        if bearer_rows is None:
            return None
        return [
            TenantNumber(phone_number=r["phone_number"], friendly_name=r["friendly_name"])
            for r in bearer_rows
        ]
    try:
        import asyncio

        from twilio.rest import Client  # noqa: PLC0415 — lazy/guarded (CI has no network)

        client = (
            Client(creds.api_key_sid, creds.api_key_secret, creds.account_sid)
            if creds.api_key_sid
            else Client(creds.account_sid, creds.auth_token)
        )
        rows = await asyncio.to_thread(lambda: client.incoming_phone_numbers.list(limit=limit))
    except ImportError:  # pragma: no cover - the SDK is a live-mode dependency
        return None
    except Exception:  # noqa: BLE001 — a listing failure degrades to manual entry
        return None
    return [
        TenantNumber(
            phone_number=str(getattr(r, "phone_number", "") or ""),
            friendly_name=str(getattr(r, "friendly_name", "") or ""),
        )
        for r in rows
        if getattr(r, "phone_number", None)
    ]


async def build_tenant_sms_sender(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> SmsSenderProtocol:
    """The sender for this tenant's outbound SMS: THEIR Twilio, or nothing (§3.1.4).

    There is deliberately no platform-Twilio fallback on this path. Falling back would
    put VantageMind's account in the customer's billing path and send from a number the
    customer never authorized — the exact failure this connection model exists to fix.
    Outside live mode the hermetic Null sender is returned, so dev/CI never reaches the
    network even with a real credential stored."""
    from app.config import get_settings

    if get_settings().integration_mode != "live":
        return NullSmsSender()
    creds = await load_tenant_credentials(session, admin_id, instance_id, store=store)
    if creds is None:
        return NullSmsSender()
    if creds.usable:
        return TwilioSmsSender(
            account_sid=creds.account_sid,
            api_key_sid=creds.api_key_sid,
            api_key_secret=creds.api_key_secret,
            auth_token=creds.auth_token,
        )
    if creds.bearer_usable:
        from app.channels.twilio_bearer import TwilioBearerSmsSender  # noqa: PLC0415

        token = await _fresh_bearer_token(session, admin_id, instance_id, store=store)
        if token:
            return TwilioBearerSmsSender(account_sid=creds.account_sid, access_token=token)
    return NullSmsSender()


# --------------------------------------------------------------------------- #
# Webhook auto-configuration (c20, live-caught 2026-08-18)
# --------------------------------------------------------------------------- #
# Nothing in the product ever configured the designated number's webhooks — no
# code and no copy. A number only answered if someone pointed its Voice/SMS URLs
# at the platform by hand in the Twilio Console, and a number CHANGE silently
# killed both channels. The customer's stored credentials authenticate the
# IncomingPhoneNumbers update, so the platform now points the webhooks itself at
# designate time (disclosed in UI copy). This is webhook ROUTING on the number
# the customer designated — the platform still never provisions numbers and
# never performs carrier registration.


@dataclass(frozen=True)
class WebhookUrls:
    voice_url: str
    sms_url: str
    status_callback_url: str


def build_webhook_urls(
    public_api_base_url: str, *, capability_token: str | None = None
) -> WebhookUrls | None:
    """The three webhook endpoints, absolute against the public API origin.

    With ``capability_token`` (C11 — tenants whose auth token we do not hold),
    each URL carries the per-connection ``cap`` parameter that inbound
    verification accepts in place of an X-Twilio-Signature it can never check."""
    base = (public_api_base_url or "").rstrip("/")
    if not base:
        return None
    suffix = ""
    if capability_token:
        from urllib.parse import quote  # noqa: PLC0415

        suffix = f"?cap={quote(capability_token, safe='')}"
    return WebhookUrls(
        voice_url=f"{base}/api/v1/webhooks/twilio/voice{suffix}",
        sms_url=f"{base}/api/v1/webhooks/twilio/sms{suffix}",
        status_callback_url=f"{base}/api/v1/webhooks/twilio/voice/status{suffix}",
    )


class TwilioWebhookConfigurator(Protocol):
    """Points a number's Voice/SMS/StatusCallback webhooks at the platform."""

    async def configure(self, e164_number: str, urls: WebhookUrls) -> bool:
        """True on success; False (never raise) when the number could not be
        configured — the caller degrades to manual guidance."""
        ...


class NullWebhookConfigurator:
    """Mock/CI and not-configured environments: configures nothing, reports it."""

    async def configure(self, e164_number: str, urls: WebhookUrls) -> bool:
        return False


class TwilioApiWebhookConfigurator:
    """The real thing: find the number in the TENANT's account, update its URLs."""

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str | None,
        api_key_sid: str | None,
        api_key_secret: str | None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._api_key_sid = api_key_sid
        self._api_key_secret = api_key_secret

    def _client(self) -> Any:  # pragma: no cover - thin SDK construction
        from twilio.rest import Client

        if self._api_key_sid and self._api_key_secret:
            return Client(self._api_key_sid, self._api_key_secret, self._account_sid)
        return Client(self._account_sid, self._auth_token)

    async def configure(self, e164_number: str, urls: WebhookUrls) -> bool:
        import asyncio  # noqa: PLC0415

        def _do() -> bool:
            client = self._client()
            numbers = client.incoming_phone_numbers.list(phone_number=e164_number, limit=1)
            if not numbers:
                return False
            numbers[0].update(
                voice_url=urls.voice_url,
                sms_url=urls.sms_url,
                status_callback=urls.status_callback_url,
            )
            return True

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001 — ANY failure degrades to manual guidance
            logger.warning("twilio_webhook_autoconfig_error error=%s", type(exc).__name__)
            return False


async def build_tenant_webhook_configurator(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    store: SecretStore | None = None,
) -> TwilioWebhookConfigurator:
    """The configurator for this tenant's account, or the honest Null.

    Real only when ALL THREE hold: live mode, usable stored credentials, and a
    configured public API origin (settings.public_api_base_url — without it there
    is no URL to point anything at)."""
    from app.config import get_settings

    settings = get_settings()
    if settings.integration_mode != "live" or not settings.public_api_base_url:
        return NullWebhookConfigurator()
    creds = await load_tenant_credentials(session, admin_id, instance_id, store=store)
    if creds is None:
        return NullWebhookConfigurator()
    if creds.usable:
        return TwilioApiWebhookConfigurator(
            account_sid=creds.account_sid,
            auth_token=creds.auth_token,
            api_key_sid=creds.api_key_sid,
            api_key_secret=creds.api_key_secret,
        )
    if creds.bearer_usable:
        from app.channels.twilio_bearer import TwilioBearerWebhookConfigurator  # noqa: PLC0415

        token = await _fresh_bearer_token(session, admin_id, instance_id, store=store)
        if token:
            return TwilioBearerWebhookConfigurator(
                account_sid=creds.account_sid, access_token=token
            )
    return NullWebhookConfigurator()
