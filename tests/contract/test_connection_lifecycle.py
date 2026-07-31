"""Uniform connection lifecycle: DISCONNECT + SWITCH + provider choice (Decisions #5/#6/#7).

Before this pass a connection could only be connected and reconnected, so an owner who
picked the wrong account — or wanted to hand an integration back — had no way out, and the
provider behind each connection was pinned by the UI to a single vendor. These tests pin
the four properties that make the lifecycle trustworthy:

  1. disconnect DESTROYS the credential (nothing is left in the SecretStore),
  2. disconnect lands in a clean, reconnectable ``not_connected`` state,
  3. disconnect CASCADES: dependent tools/channels are switched off with a reason, in both
     the relational authorization rows and the JSONB the dashboard reads back,
  4. every lifecycle transition is AUDITED, and the provider is validated against the
     registry so an admin-supplied string can never steer the connect flow.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"life-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


def _admin_id(client) -> uuid.UUID:
    from app.auth.tokens import decode_token
    from app.config import get_settings

    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _instance_id(admin_id: uuid.UUID) -> uuid.UUID:
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from sqlalchemy import select

    async with tenant_session(admin_id) as s:
        return (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()


async def _connect_calendar(client) -> tuple[str, uuid.UUID, uuid.UUID]:
    """Connect a calendar and drive it to `connected` with a real secret on file."""
    from app.connections import service
    from app.connections.secrets import get_secret_store, secret_ref_for
    from app.db.session import tenant_session

    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)
    async with tenant_session(admin_id) as s:
        await service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=uuid.UUID(cid), user_id=None, probe_ok=True
        )
    # Stash a credential exactly as the OAuth callback would, so "disconnect destroys the
    # secret" is a real assertion rather than a vacuous one against an empty store.
    await get_secret_store().put(
        secret_ref_for(admin_id, instance_id, "calendar"), {"access_token": "cal-token-1"}
    )
    return cid, admin_id, instance_id


# --------------------------------------------------------------------------- #
# Decision #6 — provider CHOICE comes from the registry, not a hardcoded vendor.
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_providers_endpoint_offers_the_decision_6_choices(client):
    await _ready_account_with_luciel(client)
    r = await client.get("/api/v1/admin/connections/providers")
    assert r.status_code == 200
    by_type = {row["connectionType"]: row["providers"] for row in r.json()}

    assert [p["provider"] for p in by_type["crm"]] == ["hubspot", "salesforce", "custom_webhook"]
    assert [p["provider"] for p in by_type["record_source"]] == ["live_connector", "csv"]
    assert [p["provider"] for p in by_type["calendar"]] == ["google_calendar", "calendly"]
    # Decision #7: the Meta channels are registry entries like everything else.
    # §3.8.1: one Facebook grant powers WhatsApp AND Messenger; Instagram messaging is a
    # separate Meta app with its own login, so it is its own connection type.
    assert [p["provider"] for p in by_type["channel_auth"]] == ["meta"]
    assert [p["provider"] for p in by_type["instagram_auth"]] == ["instagram"]

    # Every option carries what the UI needs to render the choice.
    hubspot = next(p for p in by_type["crm"] if p["provider"] == "hubspot")
    assert hubspot["displayName"] == "HubSpot"
    assert hubspot["authKind"] == "oauth" and hubspot["helpText"]
    # A non-OAuth provider advertises the fields its form must collect.
    webhook = next(p for p in by_type["crm"] if p["provider"] == "custom_webhook")
    assert webhook["authKind"] == "credential_form"
    assert [f["name"] for f in webhook["credentialFields"]] == ["url", "auth_header"]
    assert next(f for f in webhook["credentialFields"] if f["name"] == "auth_header")["secret"]
    # Decision #9: knowledge sources declare the scope the owner may narrow them to.
    drive = next(p for p in by_type["knowledge_source"] if p["provider"] == "google_drive")
    assert drive["scopeKind"] == "drive_folders"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_providers_endpoint_filters_by_type_and_leaks_no_secret(client):
    from app.config import get_settings

    await _ready_account_with_luciel(client)
    r = await client.get("/api/v1/admin/connections/providers?connection_type=calendar")
    assert r.status_code == 200
    assert [row["connectionType"] for row in r.json()] == ["calendar"]
    # §3.8.3: `configured` is a BOOLEAN derived from the platform credential; the
    # credential itself must never appear on this surface.
    body = r.text
    assert "clientSecret" not in body and "client_secret" not in body
    assert get_settings().jwt_signing_key not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_calendar_is_configured_by_the_shared_google_client(client, monkeypatch):
    """Over the wire the owner sees Google Calendar become connectable as soon as the
    ONE Google OAuth app is registered — there is no separate calendar credential to
    provision, because a Google client is per-app, not per-API.

    Calendly is the control: it shares the connection type but not the vendor, so it
    must stay honest-disabled rather than ride Google's credential."""
    from app.config import Settings
    from app.connections import registry as registry_module

    settings = Settings(
        google_client_id="test-google-client.apps.googleusercontent.com",
        google_client_secret="test-secret",  # noqa: S106 — placeholder, not a real secret
        google_redirect_uri="https://api.vantagemind.ai/api/v1/connections/google/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)

    await _ready_account_with_luciel(client)
    r = await client.get("/api/v1/admin/connections/providers?connection_type=calendar")
    assert r.status_code == 200
    by_provider = {p["provider"]: p for p in r.json()[0]["providers"]}
    assert by_provider["google_calendar"]["configured"] is True
    assert by_provider["calendly"]["configured"] is False
    # The same grant still configures Drive — sharing must not have moved the credential.
    kn = await client.get("/api/v1/admin/connections/providers?connection_type=knowledge_source")
    drive = next(p for p in kn.json()[0]["providers"] if p["provider"] == "google_drive")
    assert drive["configured"] is True
    # §3.8.3: still only a boolean crosses the wire.
    assert "test-secret" not in r.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connect_rejects_a_provider_the_registry_does_not_offer(client):
    """An admin-supplied provider string steers a real OAuth endpoint, so a type that
    HAS a choice only accepts registered members of it (Decision #6)."""
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "crm", "provider": "not-a-real-crm"},
    )
    assert r.status_code == 422
    assert (await client.get("/api/v1/admin/connections")).json() == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connect_accepts_each_offered_crm_provider(client):
    """All three Decision-#6 CRM providers connect on the SAME surface — including the
    custom webhook, whose non-OAuth auth class yields a client form, not a consent URL."""
    await _ready_account_with_luciel(client)
    for provider, expected_key in (
        ("hubspot", "authorizeUrl"),
        ("salesforce", "authorizeUrl"),
        ("custom_webhook", "requiresClientForm"),
    ):
        r = await client.post(
            "/api/v1/admin/connections",
            json={"connectionType": "crm", "provider": provider},
        )
        assert r.status_code == 200, provider
        assert r.json().get(expected_key), (provider, r.json())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_channel_connect_start_uses_meta_oauth(client):
    """Decision #7: the messaging channels get a REAL connect flow through the registry —
    the same uniform surface, each with its own dialog endpoint and channel scopes.

    Two grants, not one: Facebook's dialog covers WhatsApp + Messenger, and Instagram
    messaging rides Business Login for Instagram on instagram.com."""
    from app.connections import oauth_flow

    await _ready_account_with_luciel(client)
    for connection_type, provider, host, scope_probe in (
        ("channel_auth", "meta", "https://www.facebook.com/", "whatsapp_business_messaging"),
        ("channel_auth", "meta", "https://www.facebook.com/", "pages_messaging"),
        (
            "instagram_auth",
            "instagram",
            "https://www.instagram.com/",
            "instagram_business_manage_messages",
        ),
    ):
        spec = oauth_flow.authorize_spec(provider)
        assert spec is not None, provider
        assert spec.endpoint.startswith(host)
        assert scope_probe in spec.scopes
        r = await client.post(
            "/api/v1/admin/connections",
            json={"connectionType": connection_type, "provider": provider},
        )
        assert r.status_code == 200, provider


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_facebook_dialog_never_requests_instagram_scopes_again(client):
    """Regression floor for a live outage: Facebook rejected the WHOLE authorize request
    with "Invalid Scopes: instagram_basic, instagram_manage_messages" — under the use-case
    app model those permissions belong to the separate Instagram app, so asking for them on
    the Facebook dialog does not degrade to a partial grant, it breaks Meta connect
    entirely. Instagram messaging scopes must only ever appear on the instagram client."""
    from app.connections import oauth_flow

    meta = oauth_flow.authorize_spec("meta")
    assert meta is not None
    assert "instagram" not in meta.scopes
    assert set(meta.scopes.split(",")) == {
        "whatsapp_business_messaging",
        "whatsapp_business_management",
        "pages_messaging",
        "pages_show_list",
        "business_management",
    }

    instagram = oauth_flow.authorize_spec("instagram")
    assert instagram is not None
    assert set(instagram.scopes.split(",")) == {
        "instagram_business_basic",
        "instagram_business_manage_messages",
    }


# --------------------------------------------------------------------------- #
# Decision #5b — DISCONNECT: secret destroyed, clean state, dependents handled,
# audit written.
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnect_clears_secret_and_lands_in_clean_reconnectable_state(client):
    from app.connections.secrets import get_secret_store, secret_ref_for

    await _ready_account_with_luciel(client)
    cid, admin_id, instance_id = await _connect_calendar(client)
    ref = secret_ref_for(admin_id, instance_id, "calendar")
    assert await get_secret_store().get(ref) is not None, "precondition: a secret is on file"

    r = await client.post(f"/api/v1/admin/connections/{cid}/disconnect")
    assert r.status_code == 200
    body = r.json()
    assert body["secretDeleted"] is True
    assert body["connection"]["status"] == "not_connected"
    assert body["connection"]["statusDetail"] == "Not connected"

    # The credential is GONE — that is the point of the verb.
    assert await get_secret_store().get(ref) is None

    # Unlike DELETE (terminal `revoked`), the row survives and is still listed, so the
    # owner can reconnect from where they left off.
    rows = (await client.get("/api/v1/admin/connections")).json()
    assert [x["connectionId"] for x in rows] == [cid]
    assert rows[0]["status"] == "not_connected"
    reconnect = await client.post(f"/api/v1/admin/connections/{cid}/reconnect")
    assert reconnect.status_code == 200


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnect_sweeps_the_pending_connect_attempt(client):
    """A parked OAuth state would let a callback still in flight re-credential a
    connection the owner just disconnected, so disconnect must clear it."""
    from app.db.session import tenant_session
    from app.models.connection import InstanceConnection
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    admin_id = _admin_id(client)

    async def _config() -> dict:
        async with tenant_session(admin_id) as s:
            row = (
                await s.execute(
                    select(InstanceConnection).where(
                        InstanceConnection.connection_id == uuid.UUID(cid)
                    )
                )
            ).scalar_one()
            return dict(row.non_secret_config or {})

    # Force a pending attempt onto the row the way a configured OAuth start would.
    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == uuid.UUID(cid))
            )
        ).scalar_one()
        row.non_secret_config = {
            **(row.non_secret_config or {}),
            "oauth_state_jti": "pending-jti",
            "pending_provider": "calendly",
        }
    assert (await _config()).get("oauth_state_jti") == "pending-jti"

    await client.post(f"/api/v1/admin/connections/{cid}/disconnect")
    cfg = await _config()
    assert "oauth_state_jti" not in cfg
    assert "pending_provider" not in cfg


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnect_keeps_non_secret_operational_config(client):
    """Disconnect destroys CREDENTIALS, not the owner's own data: a BYO phone number is
    non-secret operational config, and making them retype it would be gratuitous."""
    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={
            "connectionType": "sms_sender",
            "provider": "twilio",
            "phoneNumber": "+14155550111",
        },
    )
    rows = (await client.get("/api/v1/admin/connections")).json()
    sms = next(c for c in rows if c["connectionType"] == "sms_sender")

    r = await client.post(f"/api/v1/admin/connections/{sms['connectionId']}/disconnect")
    assert r.status_code == 200
    assert r.json()["connection"]["nonSecretConfig"]["destination"] == "+14155550111"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnecting_the_calendar_disables_scheduling_with_a_reason(client):
    """Decision #5b cascade: leaving scheduling switched ON after the calendar is gone
    would show the owner a healthy toggle over a capability that fails at every dispatch.
    Both stores of tool state must move together — the JSONB the dashboard reads AND the
    authorization rows the broker's default-deny gate reads."""
    from app.db.session import tenant_session
    from app.models.tool_auth import InstanceToolAuthorization
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    cid, admin_id, instance_id = await _connect_calendar(client)

    # Owner switches the whole scheduling capability on.
    scheduling = [
        "check_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    ]
    put = await client.put(
        "/api/v1/admin/luciel/tools",
        json={"tools": [{"id": t, "enabled": True} for t in scheduling]},
    )
    assert put.status_code == 200

    r = await client.post(f"/api/v1/admin/connections/{cid}/disconnect")
    assert r.status_code == 200
    assert set(r.json()["disabledTools"]) == set(scheduling)

    # (a) The JSONB the dashboard reads back is off, and says WHY.
    tools = {t["id"]: t for t in (await client.get("/api/v1/admin/luciel")).json()["tools"]}
    for tool_id in scheduling:
        assert tools[tool_id]["enabled"] is False, tool_id
        assert tools[tool_id]["disabledReason"] == "calendar_disconnected", tool_id

    # (b) The broker's authorization rows are off too, so dispatch is default-denied.
    async with tenant_session(admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(InstanceToolAuthorization).where(
                        InstanceToolAuthorization.instance_id == instance_id,
                        InstanceToolAuthorization.tool_id.in_(scheduling),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows and all(row.enabled is False for row in rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnect_writes_lifecycle_and_secret_cleanup_audit(client):
    """Every lifecycle action is audited (spec non-negotiable), and the cleanup audit
    carries only the secret REF plus the result — never secret material (§3.8.3 rule 4)."""
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    cid, admin_id, _instance_id = await _connect_calendar(client)
    # A dependent must actually be ON for the cascade to have anything to disable.
    await client.put(
        "/api/v1/admin/luciel/tools", json={"tools": [{"id": "book_appointment", "enabled": True}]}
    )
    await client.post(f"/api/v1/admin/connections/{cid}/disconnect")

    async with tenant_session(admin_id) as s:
        events = (
            (await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin_id)))
            .scalars()
            .all()
        )
    status_changes = [
        e
        for e in events
        if e.event_type == "connection_status_changed"
        and (e.event_payload or {}).get("reason") == "disconnect"
    ]
    assert len(status_changes) == 1
    assert status_changes[0].event_payload["new_status"] == "not_connected"

    cleanups = [e for e in events if e.event_type == "connection_secret_cleanup"]
    assert cleanups and cleanups[-1].event_payload["deleted"] is True
    assert "cal-token-1" not in str(cleanups[-1].event_payload)

    cascades = [e for e in events if (e.event_payload or {}).get("action") == "dependents_disabled"]
    assert cascades, "the cascade itself is an audited config change"
    assert "book_appointment" in cascades[-1].event_payload["tools"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnect_missing_connection_is_404(client):
    await _ready_account_with_luciel(client)
    r = await client.post(f"/api/v1/admin/connections/{uuid.uuid4()}/disconnect")
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_lifecycle_endpoints_require_auth(client):
    cid = uuid.uuid4()
    for path, body in (
        (f"/api/v1/admin/connections/{cid}/disconnect", None),
        (f"/api/v1/admin/connections/{cid}/switch", {"provider": "calendly"}),
    ):
        r = await client.post(path, json=body)
        assert r.status_code in (401, 403), path
    assert (await client.get("/api/v1/admin/connections/providers")).status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Decision #5c — SWITCH: disconnect + reconnect against a different provider.
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_switch_destroys_old_credential_and_repoints_provider(client):
    """Switch is for 'this is the wrong account'. The old credential goes FIRST — holding
    it would either block the new consent or keep serving from the account being left."""
    from app.connections.secrets import get_secret_store, secret_ref_for

    await _ready_account_with_luciel(client)
    cid, admin_id, instance_id = await _connect_calendar(client)
    ref = secret_ref_for(admin_id, instance_id, "calendar")
    assert await get_secret_store().get(ref) is not None

    r = await client.post(f"/api/v1/admin/connections/{cid}/switch", json={"provider": "calendly"})
    assert r.status_code == 200
    assert "authorizeUrl" in r.json()

    assert await get_secret_store().get(ref) is None, "old credential must not survive a switch"
    row = (await client.get("/api/v1/admin/connections")).json()[0]
    assert row["provider"] == "calendly"
    # Awaiting the new consent — honestly NOT connected, never a stale `connected`.
    assert row["status"] == "unconfigured"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_switch_without_provider_changes_account_on_same_provider(client):
    await _ready_account_with_luciel(client)
    cid, _admin_id, _instance_id = await _connect_calendar(client)

    r = await client.post(f"/api/v1/admin/connections/{cid}/switch", json={})
    assert r.status_code == 200
    row = (await client.get("/api/v1/admin/connections")).json()[0]
    assert row["provider"] == "google_calendar"
    assert row["status"] == "unconfigured"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_switch_rejects_a_provider_from_another_connection_type(client):
    """The registry is per-type: a CRM provider is not a valid calendar, and accepting one
    would build an authorize URL against the wrong vendor's scopes."""
    await _ready_account_with_luciel(client)
    cid, _admin_id, _instance_id = await _connect_calendar(client)

    r = await client.post(f"/api/v1/admin/connections/{cid}/switch", json={"provider": "hubspot"})
    assert r.status_code == 422
    row = (await client.get("/api/v1/admin/connections")).json()[0]
    assert row["provider"] == "google_calendar", "a rejected switch must change nothing"
    assert row["status"] == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_switch_audits_the_provider_change(client):
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    cid, admin_id, _instance_id = await _connect_calendar(client)
    await client.post(f"/api/v1/admin/connections/{cid}/switch", json={"provider": "calendly"})

    async with tenant_session(admin_id) as s:
        events = (
            (await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin_id)))
            .scalars()
            .all()
        )
    switches = [
        e
        for e in events
        if e.event_type == "connection_status_changed"
        and (e.event_payload or {}).get("reason") == "switch"
    ]
    assert len(switches) == 1
    assert switches[0].event_payload["old_provider"] == "google_calendar"
    assert switches[0].event_payload["new_provider"] == "calendly"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_switch_missing_connection_is_404(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        f"/api/v1/admin/connections/{uuid.uuid4()}/switch", json={"provider": "calendly"}
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Decision #7 — the GENERIC OAuth code-for-token callback. Meta's channel_auth
# connect could previously reach a consent screen but never leave `unconfigured`,
# because a callback existed only for knowledge sync. One route now completes any
# registered OAuth provider, on any connection type, with no per-provider branch.
# --------------------------------------------------------------------------- #
def _meta_settings():
    from app.config import Settings

    return Settings(
        meta_app_id="meta-cid",
        meta_app_secret="meta-csecret",  # noqa: S106 — test fixture, not a real secret
        meta_redirect_uri="https://api.example.com/meta-callback",
    )


def _configure_meta(monkeypatch) -> None:
    """Point every settings read the flow makes at a configured Meta app.

    The registry resolves the OAuth app credential (Decision #6) and the exchange reads
    the same config, so both modules have to see it or connect-start silently degrades
    to `requiresClientForm`."""
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = _meta_settings()
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_meta_transport(expected_code: str, monkeypatch):
    """Stand in for graph.facebook.com's token endpoint, asserting the registry's
    exchange_endpoint is what gets called."""
    import app.api.v1.admin.connections as connections_api
    import httpx

    class FakeTokenTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def post(self, url, *, data=None, headers=None):
            self.calls.append((url, dict(data or {})))
            assert url == "https://graph.facebook.com/v19.0/oauth/access_token"
            assert data["code"] == expected_code
            return httpx.Response(
                200,
                json={"access_token": "meta-tok-1", "expires_in": 5184000},
                request=httpx.Request("POST", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    transport = FakeTokenTransport()
    monkeypatch.setattr(connections_api, "oauth_transport", lambda: transport)
    return transport


def _state_from_authorize_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["state"][0]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_oauth_callback_stores_the_token_and_connects(client, monkeypatch):
    """The full Meta round trip through the public API: connect-start mints the state,
    the callback redeems the code, and the token lands in the SecretStore under the
    CHANNEL_AUTH ref — not the knowledge ref the old callback hardcoded (§3.8.3)."""
    from app.connections.secrets import get_secret_store, secret_ref_for

    _configure_meta(monkeypatch)
    transport = _fake_meta_transport("meta-code-1", monkeypatch)

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)

    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "channel_auth", "provider": "meta"},
    )
    assert rs.status_code == 200, rs.text
    authorize_url = rs.json()["authorizeUrl"]
    assert authorize_url, rs.json()

    listed = (await client.get("/api/v1/admin/connections")).json()
    conn = next(c for c in listed if c["connectionType"] == "channel_auth")
    assert conn["status"] == "unconfigured"

    rcb = await client.post(
        f"/api/v1/admin/connections/{conn['connectionId']}/oauth-callback",
        json={"code": "meta-code-1", "state": _state_from_authorize_url(authorize_url)},
    )
    assert rcb.status_code == 200, rcb.text
    body = rcb.json()
    assert body["status"] == "connected"
    assert body["statusDetail"] is None
    # Secret material must never ride the wire (§3.8.3).
    assert "secretRef" not in body
    assert "access_token" not in rcb.text

    stored = await get_secret_store().get(secret_ref_for(admin_id, instance_id, "channel_auth"))
    assert stored is not None, "the callback must persist the token for the channel"
    assert stored["access_token"] == "meta-tok-1"
    assert len(transport.calls) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_oauth_callback_audits_the_status_change(client, monkeypatch):
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select

    _configure_meta(monkeypatch)
    _fake_meta_transport("meta-code-2", monkeypatch)

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "channel_auth", "provider": "meta"},
    )
    state = _state_from_authorize_url(rs.json()["authorizeUrl"])
    listed = (await client.get("/api/v1/admin/connections")).json()
    cid = next(c["connectionId"] for c in listed if c["connectionType"] == "channel_auth")

    r = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback",
        json={"code": "meta-code-2", "state": state},
    )
    assert r.status_code == 200, r.text

    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin_id))
        ).scalars()
        connects = [
            e
            for e in rows
            if e.event_type == "connection_status_changed"
            and (e.event_payload or {}).get("reason") == "oauth_callback_token_exchanged"
        ]
    assert len(connects) == 1
    assert connects[0].event_payload["new_status"] == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_oauth_callback_rejects_a_replayed_state(client, monkeypatch):
    """``state`` is single-use (§5.1): it is consumed BEFORE the exchange, so a second
    callback carrying the same state finds no live attempt to ride. Without this a site
    that can drive the admin's browser here could graft its own account onto the tenant."""
    _configure_meta(monkeypatch)
    _fake_meta_transport("meta-code-3", monkeypatch)

    await _ready_account_with_luciel(client)
    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "channel_auth", "provider": "meta"},
    )
    state = _state_from_authorize_url(rs.json()["authorizeUrl"])
    listed = (await client.get("/api/v1/admin/connections")).json()
    cid = next(c["connectionId"] for c in listed if c["connectionType"] == "channel_auth")
    path = f"/api/v1/admin/connections/{cid}/oauth-callback"

    body = {"code": "meta-code-3", "state": state}
    assert (await client.post(path, json=body)).status_code == 200
    replay = await client.post(path, json=body)
    assert replay.status_code == 422, replay.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_oauth_callback_surfaces_a_rejected_code_as_conflict(client, monkeypatch):
    """A provider that refuses the code is a clean 409 with the reason recorded on the
    row — never a 500, and never a connection left claiming to be connected."""
    import app.api.v1.admin.connections as connections_api
    import httpx

    class FailingTokenTransport:
        async def post(self, url, *, data=None, headers=None):
            return httpx.Response(
                400,
                json={"error": {"message": "This authorization code has expired."}},
                request=httpx.Request("POST", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    _configure_meta(monkeypatch)
    monkeypatch.setattr(connections_api, "oauth_transport", lambda: FailingTokenTransport())

    await _ready_account_with_luciel(client)
    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "channel_auth", "provider": "meta"},
    )
    state = _state_from_authorize_url(rs.json()["authorizeUrl"])
    listed = (await client.get("/api/v1/admin/connections")).json()
    cid = next(c["connectionId"] for c in listed if c["connectionType"] == "channel_auth")

    r = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback",
        json={"code": "expired", "state": state},
    )
    assert r.status_code == 409, r.text

    after = (await client.get("/api/v1/admin/connections")).json()
    row = next(c for c in after if c["connectionType"] == "channel_auth")
    assert row["status"] != "connected"
    assert "oauth_exchange_failed" in (row["statusDetail"] or "")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oauth_callback_rejects_a_non_oauth_provider_and_a_foreign_id(client, monkeypatch):
    """The route is generic, not universal: a credential-form provider has no code to
    redeem, and a connection id belonging to someone else is simply not found."""
    _configure_meta(monkeypatch)

    await _ready_account_with_luciel(client)
    r404 = await client.post(
        f"/api/v1/admin/connections/{uuid.uuid4()}/oauth-callback",
        json={"code": "c", "state": "s"},
    )
    assert r404.status_code == 404

    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "crm", "provider": "custom_webhook"},
    )
    listed = (await client.get("/api/v1/admin/connections")).json()
    cid = next(c["connectionId"] for c in listed if c["connectionType"] == "crm")
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback", json={"code": "c", "state": "s"}
    )
    assert r.status_code == 422, r.text
    assert "no OAuth callback" in r.text


# --- Decision #7: which Meta asset this Luciel answers on ------------------------- #
# The OAuth callback proves the owner authorized us and flips the row to `connected`, but
# inbound routing resolves a tenant SOLELY by non_secret_config.destination. Until that is
# bound, a Meta channel that looks connected receives nothing — so binding is the step
# that actually finishes the connect, and these tests pin it end to end.


async def _connected_meta(client, monkeypatch, provider: str = "meta") -> str:
    """Drive a Meta channel_auth connection to `connected` and return its id."""
    _configure_meta(monkeypatch)
    code = f"meta-{uuid.uuid4().hex[:6]}"
    _fake_meta_transport(code, monkeypatch)
    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "channel_auth", "provider": provider},
    )
    state = _state_from_authorize_url(rs.json()["authorizeUrl"])
    listed = (await client.get("/api/v1/admin/connections")).json()
    cid = next(c["connectionId"] for c in listed if c["connectionType"] == "channel_auth")
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback", json={"code": code, "state": state}
    )
    assert r.status_code == 200, r.text
    return cid


async def _resolves_to(admin_id: uuid.UUID, destination: str) -> uuid.UUID | None:
    from app.channels import routing
    from app.db.session import tenant_session

    async with tenant_session(admin_id) as s:
        return await routing.owner_of_destination(s, "channel_auth", destination)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_binding_the_destination_is_what_makes_an_inbound_meta_message_routable(
    client, monkeypatch
):
    """A token alone cannot deliver a message: routing needs the destination id."""
    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)

    phone_number_id = f"pnid-{uuid.uuid4().hex[:10]}"
    # Authorized but not yet bound: no tenant owns the id, so inbound cannot be routed.
    assert await _resolves_to(admin_id, phone_number_id) is None

    r = await client.put(
        f"/api/v1/admin/connections/{cid}/destination", json={"destination": phone_number_id}
    )
    assert r.status_code == 200, r.text
    assert r.json()["nonSecretConfig"]["destination"] == phone_number_id
    # Secret hygiene is unchanged by this write (§3.8.3).
    assert "secretRef" not in r.text

    assert await _resolves_to(admin_id, phone_number_id) == admin_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_destination_another_account_already_holds_is_refused(client, monkeypatch):
    """Uniqueness here is a tenant boundary, not tidiness.

    ``channel_resolve_destination`` returns the FIRST matching row, so if a second account
    could claim an id the first already holds, whose conversations arrive where becomes a
    race — one tenant could capture another's inbound messages. The claim is refused."""
    await _ready_account_with_luciel(client)
    first_admin = _admin_id(client)
    first_cid = await _connected_meta(client, monkeypatch)
    shared = f"pnid-{uuid.uuid4().hex[:10]}"
    assert (
        await client.put(
            f"/api/v1/admin/connections/{first_cid}/destination", json={"destination": shared}
        )
    ).status_code == 200

    # A DIFFERENT account (fresh signup replaces the session cookie) tries to claim it.
    await _ready_account_with_luciel(client)
    second_admin = _admin_id(client)
    assert second_admin != first_admin
    second_cid = await _connected_meta(client, monkeypatch, provider="meta")

    clash = await client.put(
        f"/api/v1/admin/connections/{second_cid}/destination", json={"destination": shared}
    )
    assert clash.status_code == 409, clash.text
    assert "another account" in clash.text

    # The refusal left the first tenant's binding intact and the second's unbound.
    assert await _resolves_to(first_admin, shared) == first_admin
    row = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionId"] == second_cid
    )
    assert (row["nonSecretConfig"] or {}).get("destination") is None

    # Its OWN id is still claimable — the rule is about collisions, not about locking a
    # second tenant out of the channel.
    own = f"pnid-{uuid.uuid4().hex[:10]}"
    ok = await client.put(
        f"/api/v1/admin/connections/{second_cid}/destination", json={"destination": own}
    )
    assert ok.status_code == 200, ok.text
    assert await _resolves_to(second_admin, own) == second_admin


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_rebinding_to_a_different_number_is_audited_with_both_values(client, monkeypatch):
    """Moving Luciel to another number reroutes a live channel, so the change — and what
    it moved away from — has to be in the audit log, not just the new value."""
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)
    first, second = f"pnid-a-{uuid.uuid4().hex[:6]}", f"pnid-b-{uuid.uuid4().hex[:6]}"
    path = f"/api/v1/admin/connections/{cid}/destination"

    assert (await client.put(path, json={"destination": first})).status_code == 200
    assert (await client.put(path, json={"destination": second})).status_code == 200

    # The old id no longer routes anywhere; the new one routes here.
    assert await _resolves_to(admin_id, first) is None
    assert await _resolves_to(admin_id, second) == admin_id

    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin_id))
        ).scalars()
        binds = [e for e in rows if (e.event_payload or {}).get("action") == "bind_destination"]
    assert len(binds) == 2
    payloads = [
        (e.event_payload["old_destination"], e.event_payload["new_destination"]) for e in binds
    ]
    assert (None, first) in payloads
    assert (first, second) in payloads


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_only_channel_connections_bind_a_destination_here(client, monkeypatch):
    """An SMS number must keep going through attach_sms_number, which also runs the
    Twilio operability + A2P gate (§3.1.6). A raw destination write here would be a way
    around that gate, so non-channel types are refused outright."""
    await _ready_account_with_luciel(client)
    _cid, _admin, _inst = await _connect_calendar(client)
    del monkeypatch

    r = await client.put(f"/api/v1/admin/connections/{_cid}/destination", json={"destination": "x"})
    assert r.status_code == 422, r.text
    assert "not bound here" in r.text

    r404 = await client.put(
        f"/api/v1/admin/connections/{uuid.uuid4()}/destination", json={"destination": "x"}
    )
    assert r404.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_empty_destination_is_rejected_at_parse_time(client, monkeypatch):
    """Binding "" would write a destination that matches nothing, leaving a channel that
    claims to be configured and still cannot receive a message."""
    await _ready_account_with_luciel(client)
    cid = await _connected_meta(client, monkeypatch)
    r = await client.put(f"/api/v1/admin/connections/{cid}/destination", json={"destination": ""})
    assert r.status_code == 422, r.text
