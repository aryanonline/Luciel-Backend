"""Contract conformance for /api/v1/admin/connections/* (Architecture §3.8).

Asserts the frozen Connection / StartConnectionResult shapes (camelCase, 204 on
DELETE, authorizeUrl vs requiresClientForm variants) AND the §3.8.3 secret-hygiene
HARD rule: no secret material ever appears in a serialized Connection or anywhere
in the API surface.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"conn-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_start_oauth_returns_authorize_url(client):
    await _ready_account_with_luciel(client)
    # calendar is OAuth-class (§3.8.5) → authorizeUrl, never requiresClientForm.
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "authorizeUrl" in body and body["authorizeUrl"]
    assert "requiresClientForm" not in body or body["requiresClientForm"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_start_non_oauth_returns_client_form(client):
    await _ready_account_with_luciel(client)
    # outbound_webhook is non-OAuth → requiresClientForm:true, no authorizeUrl.
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "outbound_webhook", "provider": "zapier"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("requiresClientForm") is True
    assert body.get("authorizeUrl") is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_list_shape_and_status(client):
    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "crm", "provider": "hubspot"},
    )
    r = await client.get("/api/v1/admin/connections")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    c = rows[0]
    # Frozen Connection field set (camelCase).
    assert set(c.keys()) <= {
        "connectionId",
        "connectionType",
        "provider",
        "status",
        "nonSecretConfig",
        "statusDetail",
        "lastHealthCheckAt",
        "createdAt",
    }
    assert c["connectionType"] == "crm"
    assert c["provider"] == "hubspot"
    assert c["status"] == "unconfigured"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_secret_fields_ever_in_serialized_connection(client):
    """HARD §3.8.3: the serialized Connection must expose NO secret-bearing field."""
    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "email_sender", "provider": "sendgrid"},
    )
    r = await client.get("/api/v1/admin/connections")
    rows = r.json()
    # The serialized key set must be a subset of the frozen, all-non-secret fields.
    # nonSecretConfig is allowed (it is, by definition, non-secret). secret_ref,
    # auth_class, tokens etc. have no slot on the wire model at all.
    allowed = {
        "connectionId",
        "connectionType",
        "provider",
        "status",
        "nonSecretConfig",
        "statusDetail",
        "lastHealthCheckAt",
        "createdAt",
    }
    for c in rows:
        assert set(c.keys()) <= allowed, f"unexpected key in Connection: {set(c.keys()) - allowed}"
    # The secret_ref pointer and any credential material must never appear anywhere.
    raw = r.text.lower()
    for forbidden in ("secret_ref", "secretref", "auth_class", "authclass", "vm/"):
        assert forbidden not in raw, f"secret-bearing token {forbidden!r} leaked into Connection"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_actual_secret_value_never_serialized(client):
    """HARD §3.8.3: even when a real secret sits in the SecretStore for a connection,
    that secret value must not appear in the serialized Connection list."""
    import uuid as _uuid

    from app.auth.tokens import decode_token
    from app.config import get_settings
    from app.connections.secrets import LocalSecretStore, secret_ref_for
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "crm", "provider": "hubspot"},
    )

    token = client.cookies.get(get_settings().session_cookie_name)
    admin_id = _uuid.UUID(decode_token(token)["sub"])
    async with tenant_session(admin_id) as s:
        instance_id = (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()

    # Stash a real secret behind the connection's ref (as the OAuth callback would).
    sentinel = "SUPER-SECRET-TOKEN-9f8e7d"
    await LocalSecretStore().put(
        secret_ref_for(admin_id, instance_id, "crm"), {"access_token": sentinel}
    )

    body = (await client.get("/api/v1/admin/connections")).text
    assert sentinel not in body, "secret material leaked into serialized Connection"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reconnect_resets_and_returns_result(client):
    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    r = await client.post(f"/api/v1/admin/connections/{cid}/reconnect")
    assert r.status_code == 200
    assert "authorizeUrl" in r.json()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_swap_keeps_old_live_until_confirm(client):
    """§3.8.7 rule B: 'Change connected account' re-initiates the connect flow against a
    new provider WITHOUT taking the current connection offline (proven-before-cutover).
    The old provider/status stay live; cutover happens only on confirm_reconnect."""
    import uuid as _uuid

    from app.auth.tokens import decode_token
    from app.config import get_settings
    from app.connections import service
    from app.db.session import tenant_session

    await _ready_account_with_luciel(client)
    # Start + drive to connected so there is a LIVE account to swap away from.
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    token = client.cookies.get(get_settings().session_cookie_name)
    admin_id = _uuid.UUID(decode_token(token)["sub"])
    async with tenant_session(admin_id) as s:
        await service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=_uuid.UUID(cid), user_id=None, probe_ok=True
        )

    # Swap to a different provider — returns authorizeUrl (calendar is OAuth-class).
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/swap", json={"provider": "microsoft_calendar"}
    )
    assert r.status_code == 200
    assert "authorizeUrl" in r.json() and r.json()["authorizeUrl"]

    # PROVEN-BEFORE-CUTOVER: before confirming, the connection is still `connected`
    # (old account stays live). The provider is re-pointed, but usability is unchanged.
    row = (await client.get("/api/v1/admin/connections")).json()[0]
    assert row["status"] == "connected"

    # After a successful confirm, it cuts over to connected on the new provider.
    async with tenant_session(admin_id) as s:
        ok, state = await service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=_uuid.UUID(cid), user_id=None, probe_ok=True
        )
    assert ok and state == "connected"
    row = (await client.get("/api/v1/admin/connections")).json()[0]
    assert row["status"] == "connected"
    assert row["provider"] == "microsoft_calendar"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_swap_failed_probe_keeps_old_account_live(client):
    """§3.8.7 rule B: if the new credential fails its health probe, the swap rolls back
    to the prior status — the old account remains live (never a mid-swap outage)."""
    import uuid as _uuid

    from app.auth.tokens import decode_token
    from app.config import get_settings
    from app.connections import service
    from app.db.session import tenant_session

    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    token = client.cookies.get(get_settings().session_cookie_name)
    admin_id = _uuid.UUID(decode_token(token)["sub"])
    async with tenant_session(admin_id) as s:
        await service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=_uuid.UUID(cid), user_id=None, probe_ok=True
        )
    await client.post(
        f"/api/v1/admin/connections/{cid}/swap", json={"provider": "microsoft_calendar"}
    )
    # Probe fails → prior status restored; old account stays live (§3.8.7 rule B).
    async with tenant_session(admin_id) as s:
        ok, _reason = await service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=_uuid.UUID(cid), user_id=None, probe_ok=False
        )
    assert not ok
    assert (await client.get("/api/v1/admin/connections")).json()[0]["status"] == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_swap_missing_404(client):
    await _ready_account_with_luciel(client)
    r = await client.post(f"/api/v1/admin/connections/{uuid.uuid4()}/swap", json={"provider": "x"})
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnect_204_and_hidden_from_list(client):
    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    rd = await client.delete(f"/api/v1/admin/connections/{cid}")
    assert rd.status_code == 204
    # §3.8.4: revoked is retained but not surfaced in the live list.
    rows = (await client.get("/api/v1/admin/connections")).json()
    assert all(c["connectionId"] != cid for c in rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnect_missing_404(client):
    await _ready_account_with_luciel(client)
    rd = await client.delete(f"/api/v1/admin/connections/{uuid.uuid4()}")
    assert rd.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connections_require_auth(client):
    # No session cookie → 401/403 (transport maps both to unauthorized).
    r = await client.get("/api/v1/admin/connections")
    assert r.status_code in (401, 403)


# --- §3.1.6a: email provisioning wire (matches Luciel-Frontend PR #13) ---------------
_EMAIL_ALLOWED_KEYS = {"mode", "emailAddress", "status", "dnsRecords"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_provisioning_absent_before_configured(client):
    await _ready_account_with_luciel(client)
    r = await client.get("/api/v1/admin/connections/email")
    assert r.status_code == 200
    assert r.json() is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_own_domain_provisioning_is_pending_with_dns(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/connections/email",
        json={"mode": "own_domain", "emailAddress": "hello@acme.com"},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) <= _EMAIL_ALLOWED_KEYS
    assert body["mode"] == "own_domain"
    assert body["emailAddress"] == "hello@acme.com"
    assert body["status"] == "pending_email_routing"
    # Own-domain requires walked-through DNS records to complete inbound routing.
    assert body["dnsRecords"] and len(body["dnsRecords"]) >= 1
    rec = body["dnsRecords"][0]
    assert set(rec.keys()) <= {"type", "host", "value", "priority"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_vm_subdomain_provisioning_is_connected(client):
    await _ready_account_with_luciel(client)
    r = await client.post("/api/v1/admin/connections/email", json={"mode": "vm_subdomain"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "vm_subdomain"
    assert body["status"] == "connected"
    assert "@" in body["emailAddress"]
    # Subdomain mode skips DNS entirely.
    assert body.get("dnsRecords") in (None, [])
    # GET reflects the provisioned identity.
    g = await client.get("/api/v1/admin/connections/email")
    assert g.json()["emailAddress"] == body["emailAddress"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_provisioning_never_leaks_secret_fields(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/connections/email",
        json={"mode": "own_domain", "emailAddress": "hi@acme.com"},
    )
    body = r.json()
    assert set(body.keys()) <= _EMAIL_ALLOWED_KEYS, f"unexpected: {set(body) - _EMAIL_ALLOWED_KEYS}"
    raw = r.text.lower()
    for forbidden in ("secret_ref", "secretref", "auth_class", "authclass", "vm/"):
        assert forbidden not in raw


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_provisioning_requires_auth(client):
    r = await client.get("/api/v1/admin/connections/email")
    assert r.status_code in (401, 403)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_reverify_without_number_is_404(client):
    """Re-verify with no attached number → 404 (nothing to re-verify)."""
    await _ready_account_with_luciel(client)
    r = await client.post("/api/v1/admin/connections/sms/reverify")
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_attach_then_reverify_is_honest_in_mock(client):
    """In mock mode the operability probe is the hermetic Null probe (no number is
    operable), so attaching a BYO number lands in not_operable_hosting_required — NOT a
    'being activated' live state — and re-verify repeatably reports the same honest
    state. This is the anti-silent-failure invariant at the HTTP surface."""
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio", "phoneNumber": "+14155550111"},
    )
    assert r.status_code == 200
    # Status lives on the ConnectionOut (GET /connections), not the StartConnectionResult.
    g = await client.get("/api/v1/admin/connections")
    sms = [c for c in g.json() if c["connectionType"] == "sms_sender"]
    assert sms and sms[0]["status"] == "not_operable_hosting_required"
    # Re-verify: still not operable (mock), repeatable, never flips to live on a click.
    rv = await client.post("/api/v1/admin/connections/sms/reverify")
    assert rv.status_code == 200
    assert rv.json().get("status") == "not_operable_hosting_required"
    rv2 = await client.post("/api/v1/admin/connections/sms/reverify")
    assert rv2.status_code == 200
    assert rv2.json().get("status") == "not_operable_hosting_required"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_reverify_requires_auth(client):
    r = await client.post("/api/v1/admin/connections/sms/reverify")
    assert r.status_code in (401, 403)
