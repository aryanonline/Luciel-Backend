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
    assert [p["provider"] for p in by_type["channel_auth"]] == ["meta_whatsapp", "meta_instagram"]

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
    """Decision #7: WhatsApp / Instagram get a REAL connect flow through the registry —
    the same uniform surface, with Meta's dialog endpoint and channel scopes."""
    from app.connections import oauth_flow

    await _ready_account_with_luciel(client)
    for provider, scope_probe in (
        ("meta_whatsapp", "whatsapp_business_messaging"),
        ("meta_instagram", "instagram_manage_messages"),
    ):
        spec = oauth_flow.authorize_spec(provider)
        assert spec is not None, provider
        assert spec.endpoint.startswith("https://www.facebook.com/")
        assert scope_probe in spec.scopes
        r = await client.post(
            "/api/v1/admin/connections",
            json={"connectionType": "channel_auth", "provider": provider},
        )
        assert r.status_code == 200, provider


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
