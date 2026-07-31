"""OAuth connect-START: real authorize URLs + single-use signed state (§5.1).

The connect flow had a token-EXCHANGE half but no initiation — ``start_connection``
handed the UI a placeholder pointing at a route that does not exist, so no admin could
ever reach a provider consent screen. These tests pin the initiation half and the CSRF
property that makes the callback safe to accept.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from app.config import Settings, get_settings
from app.connections import oauth_flow


@pytest.fixture
def configured(monkeypatch):
    """Register OAuth clients for the providers whose credentials exist.

    There is ONE Google client on purpose: Drive and Calendar are two scopes on the
    same Google Cloud OAuth app, not two apps."""
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        google_client_id="google-client",
        google_client_secret="google-secret",
        google_redirect_uri="https://api.vantagemind.ai/api/v1/connections/google/callback",
        hubspot_client_id="hs-client",
        hubspot_client_secret="hs-secret",
        hubspot_redirect_uri="https://api.vantagemind.ai/oauth/hubspot",
        salesforce_consumer_key="sf-key",
        salesforce_consumer_secret="sf-secret",
        salesforce_redirect_uri="https://api.vantagemind.ai/oauth/salesforce",
    )
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)
    # The OAuth app credential for a provider is resolved through the provider REGISTRY
    # now (Decision #6), so that is the module whose settings read must see this config.
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    return settings


def _start(provider: str) -> tuple[oauth_flow.AuthorizeStart, dict[str, list[str]]]:
    start = oauth_flow.build_authorize_start(
        provider=provider,
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
    )
    return start, parse_qs(urlparse(start.authorize_url).query)


@pytest.mark.parametrize(
    ("provider", "host"),
    [
        ("google_drive", "accounts.google.com"),
        ("google_calendar", "accounts.google.com"),
        ("hubspot", "app.hubspot.com"),
        ("salesforce", "login.salesforce.com"),
    ],
)
def test_authorize_url_points_at_the_real_provider_with_our_client(configured, provider, host):
    """The URL must be one a browser can actually follow to a consent screen — the
    old placeholder pointed at an internal route that was never implemented."""
    start, params = _start(provider)
    assert not start.unconfigured
    assert urlparse(start.authorize_url).netloc == host
    assert params["response_type"] == ["code"]
    assert params["client_id"][0]
    assert params["redirect_uri"][0].startswith("https://api.vantagemind.ai/")
    assert params["scope"][0]
    assert params["state"][0]


def test_google_requests_offline_access_for_a_refresh_token(configured):
    """Google only issues a refresh_token for an offline+consent request; without it
    the connection dies silently when the first access token expires."""
    _start_result, params = _start("google_drive")
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


def test_calendar_reuses_the_drive_google_client_with_its_own_scope(configured):
    """Calendar was unconfigurable because it demanded its OWN google_calendar_* client
    that no one ever registered, so the only calendar provider on the platform sat
    honest-disabled while a perfectly good Google app was already connected for Drive.

    A Google OAuth client is not per-API — the same client_id requests whichever scopes
    the authorize URL asks for. So the app IDENTITY (client_id + redirect) is shared and
    only the SCOPE differs; that split is what this pins."""
    from app.connections.registry import oauth_client_config

    cal, cal_params = _start("google_calendar")
    _drive, drive_params = _start("google_drive")

    assert not cal.unconfigured
    assert cal_params["client_id"] == drive_params["client_id"] == ["google-client"]
    assert cal_params["redirect_uri"] == drive_params["redirect_uri"]
    assert oauth_client_config("google_calendar") == oauth_client_config("google_drive")

    # The scope is the part that must NOT be shared.
    assert cal_params["scope"] == ["https://www.googleapis.com/auth/calendar.events"]
    assert drive_params["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]

    # A refresh token is what keeps the calendar connected past the first hour, and
    # incremental auth is what stops this grant from narrowing an existing Drive grant.
    assert cal_params["access_type"] == ["offline"]
    assert cal_params["prompt"] == ["consent"]
    assert cal_params["include_granted_scopes"] == ["true"]


def test_calendar_is_unconfigured_without_the_google_client(monkeypatch):
    """The flip must be driven by the credential, not by the registry entry existing —
    otherwise the UI offers a consent screen that cannot be reached."""
    from app.connections import registry as registry_module

    monkeypatch.setattr(registry_module, "get_settings", lambda: Settings())
    start, _params = _start("google_calendar")
    assert start.unconfigured is True
    assert start.state_jti is None


def test_calendly_stays_unconfigured_on_the_google_client(configured):
    """Sharing is per-VENDOR, not "any calendar provider": Calendly has no credential,
    so it must stay honest-disabled rather than inherit Google's."""
    start, _params = _start("calendly")
    assert start.unconfigured is True


def test_pkce_challenge_is_sent_only_to_providers_that_implement_it(configured):
    """HubSpot's authorize endpoint rejects unknown query params and has no PKCE, so
    sending a challenge fails the request outright."""
    _gd, gd_params = _start("google_drive")
    assert gd_params["code_challenge_method"] == ["S256"]
    assert gd_params["code_challenge"][0]

    _hs, hs_params = _start("hubspot")
    assert "code_challenge" not in hs_params


def test_pkce_verifier_is_derived_not_stored(configured):
    """The connection row is projected onto the wire as ``nonSecretConfig``, so a stored
    verifier would be readable by the admin's browser and PKCE would prove nothing.
    Deriving it from the jti + the server-only signing key keeps it off the wire while
    still letting the callback reproduce it."""
    start, params = _start("google_drive")
    assert start.state_jti is not None

    verifier = oauth_flow.verifier_for_jti(start.state_jti)
    assert oauth_flow.pkce_challenge(verifier) == params["code_challenge"][0]
    # Nothing the browser can see contains the verifier.
    assert verifier not in start.authorize_url


def test_unconfigured_provider_falls_back_honestly_rather_than_erroring(monkeypatch):
    """A provider with no registered OAuth client has no honest authorize URL to give.
    Failing the whole connect call would break the non-OAuth catalog entries that share
    this code path, so the row keeps a placeholder and says why."""
    from app.knowledge import providers as providers_module

    monkeypatch.setattr(providers_module, "get_settings", lambda: Settings())
    start, _params = _start("google_drive")
    assert start.unconfigured is True
    assert start.state_jti is None
    assert start.authorize_url.endswith("/authorize")


def test_state_round_trips_only_for_its_own_attempt(configured):
    admin_id, instance_id, connection_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    jti = uuid.uuid4().hex
    state = oauth_flow.mint_state(
        admin_id=admin_id,
        instance_id=instance_id,
        connection_id=connection_id,
        provider="google_drive",
        jti=jti,
    )
    oauth_flow.verify_state(state, admin_id=admin_id, connection_id=connection_id, expected_jti=jti)

    # Another tenant's admin cannot redeem it.
    with pytest.raises(oauth_flow.StateRejected):
        oauth_flow.verify_state(
            state, admin_id=uuid.uuid4(), connection_id=connection_id, expected_jti=jti
        )
    # Nor can it be pointed at a different connection.
    with pytest.raises(oauth_flow.StateRejected):
        oauth_flow.verify_state(
            state, admin_id=admin_id, connection_id=uuid.uuid4(), expected_jti=jti
        )


def test_state_is_single_use_and_required(configured):
    """A replayed callback finds no pending attempt (the jti was cleared on redemption),
    and a callback with no state at all is refused — otherwise any site that could drive
    the admin's browser here would get a code redeemed into their connection."""
    admin_id, connection_id = uuid.uuid4(), uuid.uuid4()
    jti = uuid.uuid4().hex
    state = oauth_flow.mint_state(
        admin_id=admin_id,
        instance_id=uuid.uuid4(),
        connection_id=connection_id,
        provider="google_drive",
        jti=jti,
    )

    with pytest.raises(oauth_flow.StateRejected):
        oauth_flow.verify_state(
            None, admin_id=admin_id, connection_id=connection_id, expected_jti=jti
        )
    with pytest.raises(oauth_flow.StateRejected):
        oauth_flow.verify_state(
            state, admin_id=admin_id, connection_id=connection_id, expected_jti=None
        )
    with pytest.raises(oauth_flow.StateRejected):
        oauth_flow.verify_state(
            state, admin_id=admin_id, connection_id=connection_id, expected_jti=uuid.uuid4().hex
        )


def test_forged_state_is_rejected(configured):
    """An unsigned/foreign-signed state must not pass — the signature is the only thing
    tying the callback to an attempt WE started."""
    import jwt

    admin_id, connection_id = uuid.uuid4(), uuid.uuid4()
    jti = uuid.uuid4().hex
    forged = jwt.encode(
        {"typ": "oauth_state", "sub": str(admin_id), "cid": str(connection_id), "jti": jti},
        "not-the-platform-key",
        algorithm=get_settings().jwt_algorithm,
    )
    with pytest.raises(oauth_flow.StateRejected):
        oauth_flow.verify_state(
            forged, admin_id=admin_id, connection_id=connection_id, expected_jti=jti
        )


def test_expired_state_is_rejected(configured, monkeypatch):
    """A consent click-through is minutes, not hours — a state left open indefinitely
    widens the CSRF window for no benefit."""
    from datetime import UTC, datetime, timedelta

    stale = datetime.now(UTC) - timedelta(seconds=oauth_flow.STATE_TTL_SECONDS + 60)
    monkeypatch.setattr(oauth_flow, "_now", lambda: stale)
    admin_id, connection_id = uuid.uuid4(), uuid.uuid4()
    jti = uuid.uuid4().hex
    state = oauth_flow.mint_state(
        admin_id=admin_id,
        instance_id=uuid.uuid4(),
        connection_id=connection_id,
        provider="google_drive",
        jti=jti,
    )
    monkeypatch.undo()
    with pytest.raises(oauth_flow.StateRejected):
        oauth_flow.verify_state(
            state, admin_id=admin_id, connection_id=connection_id, expected_jti=jti
        )
