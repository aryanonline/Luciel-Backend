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
from app.connections.registry import ProviderAuthorize, get_provider, oauth_client_config

STATE_TTL_SECONDS = 600  # a consent click-through is minutes, not hours
STATE_TOKEN_TYPE = "oauth_state"  # noqa: S105 — a token-type discriminator, not a secret  # nosec B105

UNCONFIGURED_DETAIL = "Action needed: this provider's OAuth client is not configured yet"


def authorize_spec(provider: str) -> ProviderAuthorize | None:
    """This provider's authorize endpoint/scopes/PKCE posture, from the registry.

    The table used to live here; it now lives in ``connections.registry`` alongside the
    provider's credential source and health class, so registering a provider is one
    entry rather than edits in three files. Read through ``get_provider`` so the
    endpoints come back resolved (the Meta Graph version is a setting)."""
    spec = get_provider(provider)
    return spec.authorize if spec is not None else None


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
class StateClaims:
    """Who a signed ``state`` says the attempt belongs to."""

    admin_id: uuid.UUID
    instance_id: uuid.UUID
    connection_id: uuid.UUID
    provider: str
    jti: str


def decode_state(state: str | None) -> StateClaims:
    """Recover (admin, instance, connection, provider) from a signature-verified state.

    The GET callback is reached by the PROVIDER redirecting the browser to a fixed
    redirect URI, so the URL carries no connection id and no admin session is required
    to be present. The signed state is therefore the only thing that can say which
    attempt this is — which is safe precisely because it is signed with the platform
    key and short-lived.

    This establishes IDENTITY only. It is not sufficient authorization: the caller must
    still ``verify_state`` against the jti parked on the row, which is what makes the
    state single-use. Raises ``StateRejected``."""
    if not state:
        raise StateRejected("Missing OAuth state.")
    s = get_settings()
    try:
        claims = jwt.decode(state, s.jwt_signing_key, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise StateRejected("Invalid or expired OAuth state.") from exc
    if claims.get("typ") != STATE_TOKEN_TYPE:
        raise StateRejected("Wrong token type for OAuth state.")
    try:
        return StateClaims(
            admin_id=uuid.UUID(str(claims["sub"])),
            instance_id=uuid.UUID(str(claims["iid"])),
            connection_id=uuid.UUID(str(claims["cid"])),
            provider=str(claims["prv"]),
            jti=str(claims["jti"]),
        )
    except (KeyError, ValueError) as exc:
        raise StateRejected("Malformed OAuth state.") from exc


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
    spec = authorize_spec(provider)
    cfg = oauth_client_config(provider)
    # authorize_ready = id + secret + a redirect URI the callback route serves (an
    # invalid one is logged at boot and named on /healthz, round 7 WP-7).
    if spec is None or not cfg.authorize_ready:
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
