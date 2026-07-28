"""OAuth connect-START: real provider authorize URLs + signed state (§3.8.3 / §5.1).

The connect flow had a token-EXCHANGE half (``knowledge.providers.exchange_code_for_token``)
but no initiation: ``start_connection`` handed the UI a placeholder pointing at a route
that does not exist, so no admin could ever reach a provider consent screen. This module
is the missing half — it builds the provider's real authorize URL with our client_id, the
scopes we actually need, PKCE, and a signed ``state``.

State is a short-TTL JWT bound to (admin, instance, connection, provider) and carrying a
``jti``. The jti and the PKCE ``code_verifier`` are parked on the connection row, so the
callback can prove the code came back for THIS connect attempt and can only be redeemed
once (``consume_state``). Without that check the callback would accept any code any site
could induce the admin's browser to deliver — CSRF straight into a tenant's connection.

Providers whose OAuth client is not registered in config keep the old internal
placeholder URL: an unconfigured provider has no honest authorize URL to give, and
failing the whole connect call would break the non-OAuth catalog entries that share this
code path. The row's ``status_detail`` says so plainly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import jwt

from app.config import get_settings
from app.knowledge.providers import oauth_client_config

STATE_TTL_SECONDS = 600  # a consent click-through is minutes, not hours
STATE_TOKEN_TYPE = "oauth_state"  # noqa: S105 — a token-type discriminator, not a secret

UNCONFIGURED_DETAIL = "Action needed: this provider's OAuth client is not configured yet"


@dataclass(frozen=True)
class ProviderAuthorize:
    """One provider's authorize endpoint + the scopes this platform requests."""

    endpoint: str
    scopes: str
    supports_pkce: bool
    # Provider-specific params that are part of OUR request, not the user's choice
    # (e.g. Google only returns a refresh_token for an offline+consent request).
    extra: dict[str, str]


PROVIDER_AUTHORIZE: dict[str, ProviderAuthorize] = {
    "google_drive": ProviderAuthorize(
        endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        scopes="https://www.googleapis.com/auth/drive.readonly",
        supports_pkce=True,
        extra={"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
    ),
    "hubspot": ProviderAuthorize(
        endpoint="https://app.hubspot.com/oauth/authorize",
        scopes="crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read",
        # HubSpot's authorize endpoint rejects unknown query params, and it does not
        # implement PKCE — sending a code_challenge fails the request outright.
        supports_pkce=False,
        extra={},
    ),
    "salesforce": ProviderAuthorize(
        endpoint="https://login.salesforce.com/services/oauth2/authorize",
        scopes="api refresh_token",
        supports_pkce=True,
        extra={},
    ),
}


def _now() -> datetime:
    return datetime.now(UTC)


def verifier_for_jti(jti: str) -> str:
    """RFC 7636 code_verifier, DERIVED from the attempt's jti + the platform signing key.

    Deriving rather than generating means the verifier never has to be stored anywhere
    the admin's browser can read (the connection row is projected onto the wire as
    ``nonSecretConfig``), yet the callback can still reproduce it from the jti alone.
    The signing key is server-only, so the verifier is unguessable from the jti."""
    key = get_settings().jwt_signing_key.encode("utf-8")
    digest = hmac.new(key, f"pkce:{jti}".encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """S256 challenge for a verifier (base64url of SHA-256, unpadded)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def mint_state(
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_id: uuid.UUID,
    provider: str,
    jti: str,
) -> str:
    """Signed, short-lived ``state`` bound to this exact connect attempt."""
    s = get_settings()
    payload = {
        "typ": STATE_TOKEN_TYPE,
        "sub": str(admin_id),
        "iid": str(instance_id),
        "cid": str(connection_id),
        "prv": provider,
        "jti": jti,
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(seconds=STATE_TTL_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_signing_key, algorithm=s.jwt_algorithm)


class StateRejected(Exception):
    """The returned ``state`` is missing, forged, expired, or for another attempt."""


def verify_state(
    state: str | None, *, admin_id: uuid.UUID, connection_id: uuid.UUID, expected_jti: str | None
) -> None:
    """Reject anything but the state WE minted for THIS connection's pending attempt.

    Raises ``StateRejected``; the caller turns that into a clean 4xx. A missing
    ``expected_jti`` means no connect attempt is pending, so there is nothing a code
    could legitimately complete."""
    if not state:
        raise StateRejected("Missing OAuth state.")
    if not expected_jti:
        raise StateRejected("No pending OAuth connect attempt for this connection.")
    s = get_settings()
    try:
        claims = jwt.decode(state, s.jwt_signing_key, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise StateRejected("Invalid or expired OAuth state.") from exc
    if claims.get("typ") != STATE_TOKEN_TYPE:
        raise StateRejected("Wrong token type for OAuth state.")
    if claims.get("sub") != str(admin_id) or claims.get("cid") != str(connection_id):
        raise StateRejected("OAuth state does not match this connection.")
    if not secrets.compare_digest(str(claims.get("jti", "")), expected_jti):
        raise StateRejected("OAuth state has already been used or superseded.")


@dataclass(frozen=True)
class AuthorizeStart:
    """What ``start_connection`` needs to persist + return for one connect attempt.

    Only the ``state_jti`` is persisted; the PKCE verifier is re-derived from it at
    callback time via ``verifier_for_jti`` so it is never written down."""

    authorize_url: str
    # None when the provider's OAuth client is unconfigured (placeholder URL returned).
    state_jti: str | None = None
    uses_pkce: bool = False
    unconfigured: bool = False


def build_authorize_start(
    *,
    provider: str,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> AuthorizeStart:
    """Build the provider consent URL for one connect attempt (§5.1).

    Falls back to the internal placeholder for providers with no registered OAuth
    client — see the module docstring for why that is a fallback and not an error."""
    spec = PROVIDER_AUTHORIZE.get(provider)
    cfg = oauth_client_config(provider)
    if spec is None or not cfg.configured or not cfg.redirect_uri:
        return AuthorizeStart(
            authorize_url=f"/api/v1/admin/connections/{connection_id}/authorize",
            unconfigured=True,
        )

    jti = uuid.uuid4().hex
    state = mint_state(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_id=connection_id,
        provider=provider,
        jti=jti,
    )
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": spec.scopes,
        "state": state,
        **spec.extra,
    }
    if spec.supports_pkce:
        params["code_challenge"] = pkce_challenge(verifier_for_jti(jti))
        params["code_challenge_method"] = "S256"
    return AuthorizeStart(
        authorize_url=f"{spec.endpoint}?{urlencode(params)}",
        state_jti=jti,
        uses_pkce=spec.supports_pkce,
    )
