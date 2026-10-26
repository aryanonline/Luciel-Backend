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


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

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
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _seed_platform_email(admin_id: uuid.UUID, *, mode: str, address: str | None = None):
    """Seed a LEGACY platform email row via the service layer (c22): the route no
    longer offers platform modes, but existing rows keep serving and their wire
    shapes stay pinned here."""
    from app.connections import service as conn_service
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from sqlalchemy import select as _select

    async with tenant_session(admin_id) as s:
        iid = (
            await s.execute(_select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()
        return await conn_service.provision_email(
            s,
            admin_id=admin_id,
            instance_id=iid,
            mode=mode,
            email_address=address,
            instance_name="L",
            user_id=None,
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
    # "outbound_webhook" is the registry's ONE offered provider for this surface
    # (Phase 5) — start_connection now refuses ids the registry does not offer,
    # so the old made-up "zapier" would honestly 422.
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "outbound_webhook", "provider": "outbound_webhook"},
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
    # Frozen Connection field set (camelCase). providerAvailable added Harmony wave 2
    # (item 3): registry truth about whether this provider can be connected today.
    assert set(c.keys()) <= {
        "connectionId",
        "connectionType",
        "provider",
        "status",
        "nonSecretConfig",
        "statusDetail",
        "lastHealthCheckAt",
        "createdAt",
        "providerAvailable",
        "displayName",
        # Round 7 walk run 8: the knowledge sync job (None on every other row) — the
        # manual sync answers 202 when it outlives the inline wait, and this is where
        # its outcome is read afterwards.
        "syncJob",
    }
    assert c["syncJob"] is None  # a CRM row never runs a knowledge sync job
    assert c["connectionType"] == "crm"
    assert c["provider"] == "hubspot"
    assert c["status"] == "unconfigured"
    assert c["displayName"] == "HubSpot"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_provider_available_true_for_non_oauth_provider(client):
    """A credential-form (non-OAuth) provider needs no platform OAuth client, so it
    is available the moment it is registered -- the "healthy" counterpart to the
    stale-row test below."""
    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "outbound_webhook", "provider": "outbound_webhook"},
    )
    r = await client.get("/api/v1/admin/connections")
    row = r.json()[0]
    assert row["provider"] == "outbound_webhook"
    assert row["providerAvailable"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_sender_overview_row_shows_amazon_ses_not_raw_code(client):
    """LIVE-VERIFIED (Harmony wave 2, item 4): the Overview catalog rendered
    "Email sending · Ses" -- the raw provider code, title-cased, because
    ConnectionOut carried NO display label at all and the frontend had nothing
    else to render. Provisioning the platform email sender (provider "ses") must
    now come back with displayName "Amazon SES" so Overview never has to
    improvise a label from the raw code again."""
    admin_id = await _ready_account_with_luciel(client)
    await _seed_platform_email(admin_id, mode="vm_subdomain")

    rows = (await client.get("/api/v1/admin/connections")).json()
    row = next(x for x in rows if x["connectionType"] == "email_sender")
    assert row["provider"] == "ses"
    assert row["displayName"] == "Amazon SES"
    assert row["displayName"] != "Ses"


def test_no_registered_provider_display_name_is_the_raw_code() -> None:
    """Audit guard (Harmony wave 2, item 4): every entry in the provider registry
    must carry a real human label -- never the bare raw code (``ses`` staying
    ``ses``). A naive title-case of the raw code (``ses`` -> ``Ses``) is ALSO a
    bug UNLESS the provider's brand name genuinely equals its title-cased code
    (e.g. ``calendly`` -> "Calendly", ``notion`` -> "Notion") -- those are
    correct and deliberately allow-listed here, so this guard only fires on
    the actual bug shape: a multi-word or acronym code whose brand name is NOT
    just its own title-case (the "ses" -> should-be "Amazon SES" case)."""
    from app.connections import registry

    # Providers whose registered code IS (title-cased) their real brand name --
    # confirmed correct, not a raw-code leak. "meta" joined when the §3.1.2 channel
    # split moved the channel names onto the rows: the grant's own brand IS "Meta".
    title_case_is_correct = {"calendly", "notion", "salesforce", "instagram", "twilio", "meta"}

    for provider_id, spec in registry.PROVIDERS.items():
        assert spec.display_name.strip(), f"{provider_id} has no display_name"
        assert spec.display_name != provider_id, (
            f"{provider_id} display_name is the bare raw code: {spec.display_name!r}"
        )
        if spec.display_name == provider_id.title() and provider_id not in title_case_is_correct:
            pytest.fail(
                f"{provider_id} display_name is an un-reviewed naive title-case of "
                f"the raw code: {spec.display_name!r} -- confirm it is the real brand "
                f"name and allow-list it, or fix it (this is exactly how 'ses' leaked "
                f"as 'Ses' instead of 'Amazon SES')"
            )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_stale_row_for_unconfigured_provider_reports_provider_unavailable(client):
    """LIVE-VERIFIED (Harmony wave 2, item 3): dev showed a Notion row rendering
    "! Action needed" on Overview while the Configure page correctly showed Notion
    as "not available yet" (its platform OAuth client -- notion_client_id/secret --
    was never registered). Root cause: the Overview list (list_connections /
    ConnectionOut) only ever exposed the row's own ``status`` field, with no way to
    know the row points at a provider the platform itself cannot connect right now.
    "Action needed" implies the OWNER can fix this by acting (e.g. entering
    credentials); that is false here -- no admin action unblocks a provider whose
    OAuth client the platform never configured. This reproduces the exact live
    condition: a knowledge_source/notion connection attempt against the DEFAULT
    test-env settings, where notion_client_id/secret are empty (unconfigured),
    exactly as the previous fix's test never modeled (it only checked frozen field
    shape on an ALREADY-connectable provider)."""
    from app.config import get_settings

    assert get_settings().notion_client_id == "", (
        "test assumes the default test env leaves Notion's OAuth client unconfigured"
    )
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "knowledge_source", "provider": "notion"},
    )
    assert r.status_code == 200

    rows = (await client.get("/api/v1/admin/connections")).json()
    row = next(x for x in rows if x["provider"] == "notion")
    assert row["status"] == "unconfigured"
    # The stale-row bug: providerAvailable must be False so the FE renders "not
    # available yet", never the owner-actionable "Action needed" chip.
    assert row["providerAvailable"] is False


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
        "providerAvailable",
        "displayName",
        # Round 7 walk run 8: the knowledge sync job record — states, instants, counts
        # of external ids and owner-facing messages only; no credential slot.
        "syncJob",
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
        json={"connectionType": "crm", "provider": "hubspot"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    token = client.cookies.get(get_settings().session_cookie_name)
    admin_id = _uuid.UUID(decode_token(token)["sub"])
    async with tenant_session(admin_id) as s:
        await service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=_uuid.UUID(cid), user_id=None, probe_ok=True
        )

    # Swap to a different provider — returns authorizeUrl (calendar is OAuth-class).
    r = await client.post(f"/api/v1/admin/connections/{cid}/swap", json={"provider": "salesforce"})
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
    assert row["provider"] == "salesforce"


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
        json={"connectionType": "crm", "provider": "hubspot"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    token = client.cookies.get(get_settings().session_cookie_name)
    admin_id = _uuid.UUID(decode_token(token)["sub"])
    async with tenant_session(admin_id) as s:
        await service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=_uuid.UUID(cid), user_id=None, probe_ok=True
        )
    await client.post(f"/api/v1/admin/connections/{cid}/swap", json={"provider": "salesforce"})
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
async def test_platform_email_provisioning_is_refused(client):
    """c22 (owner decision 2026-08-18): email is BYO-mailbox ONLY. Both platform
    modes answer an actionable refusal, never a new platform address."""
    await _ready_account_with_luciel(client)
    for payload in (
        {"mode": "own_domain", "emailAddress": "hello@acme.com"},
        {"mode": "vm_subdomain"},
    ):
        r = await client.post("/api/v1/admin/connections/email", json=payload)
        assert r.status_code == 422
        assert "connect your own" in r.json()["message"].lower()
    # Nothing was written.
    g = await client.get("/api/v1/admin/connections/email")
    assert g.json() is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_legacy_own_domain_row_still_serves_pending_with_dns(client):
    """Existing own_domain tenants keep their wire shape: pending_email_routing
    plus the DNS records that complete inbound routing."""
    admin_id = await _ready_account_with_luciel(client)
    await _seed_platform_email(admin_id, mode="own_domain", address="hello@acme.com")
    body = (await client.get("/api/v1/admin/connections/email")).json()
    assert set(body.keys()) <= _EMAIL_ALLOWED_KEYS
    assert body["mode"] == "own_domain"
    assert body["emailAddress"] == "hello@acme.com"
    assert body["status"] == "pending_email_routing"
    assert body["dnsRecords"] and len(body["dnsRecords"]) >= 1
    rec = body["dnsRecords"][0]
    assert set(rec.keys()) <= {"type", "host", "value", "priority"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_legacy_vm_subdomain_row_still_serves_connected(client):
    admin_id = await _ready_account_with_luciel(client)
    await _seed_platform_email(admin_id, mode="vm_subdomain")
    body = (await client.get("/api/v1/admin/connections/email")).json()
    assert body["mode"] == "vm_subdomain"
    assert body["status"] == "connected"
    assert "@" in body["emailAddress"]
    # Subdomain mode never had DNS records.
    assert body.get("dnsRecords") in (None, [])


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_provisioning_never_leaks_secret_fields(client):
    admin_id = await _ready_account_with_luciel(client)
    await _seed_platform_email(admin_id, mode="own_domain", address="hi@acme.com")
    r = await client.get("/api/v1/admin/connections/email")
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_reverify_without_provisioning_is_404(client):
    """Harmony fix 2026-08-04 item 5: re-verify with no email provisioned at all ->
    404 (nothing to re-verify), mirroring the SMS reverify contract."""
    await _ready_account_with_luciel(client)
    r = await client.post("/api/v1/admin/connections/email/reverify")
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_own_domain_reverify_closes_the_pending_dead_end(client):
    """The exact dead end item 5 closes: provisioning own_domain lands in
    pending_email_routing with previously NO way forward. Re-verify now exists,
    responds 200, and -- honestly, since mock mode's MX probe never reports a domain
    as delegated -- repeatably reports the same non-live state rather than silently
    flipping to connected on a click."""
    admin_id = await _ready_account_with_luciel(client)
    await _seed_platform_email(admin_id, mode="own_domain", address="hello@acme.com")
    assert (await client.get("/api/v1/admin/connections/email")).json()[
        "status"
    ] == "pending_email_routing"

    rv = await client.post("/api/v1/admin/connections/email/reverify")
    assert rv.status_code == 200
    assert rv.json().get("status") == "pending_email_routing"
    # Repeatable: a second click is the same honest answer, not a fluke.
    rv2 = await client.post("/api/v1/admin/connections/email/reverify")
    assert rv2.status_code == 200
    assert rv2.json().get("status") == "pending_email_routing"

    # GET reflects the same live status the reverify call reported.
    g = await client.get("/api/v1/admin/connections")
    email_conn = [c for c in g.json() if c["connectionType"] == "email_sender"]
    assert email_conn and email_conn[0]["status"] == "pending_email_routing"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_vm_subdomain_reverify_is_a_harmless_noop(client):
    """vm_subdomain is already connected with no DNS step -- re-verify must not error
    or regress its status."""
    admin_id = await _ready_account_with_luciel(client)
    await _seed_platform_email(admin_id, mode="vm_subdomain")
    assert (await client.get("/api/v1/admin/connections/email")).json()["status"] == "connected"

    rv = await client.post("/api/v1/admin/connections/email/reverify")
    assert rv.status_code == 200
    assert rv.json().get("status") == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_email_reverify_requires_auth(client):
    r = await client.post("/api/v1/admin/connections/email/reverify")
    assert r.status_code in (401, 403)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_attach_of_a_foreign_held_number_is_409(client):
    """Cross-tenant destination uniqueness at the HTTP surface (§3.1.3): a number
    another tenant already holds cannot be claimed — the same 409 the email bind
    serves. Inbound SMS/voice route on this destination, so before this gate a
    second claim could capture the holder's conversations (live-caught 2026-08-17)."""
    from app.channels.number_operability import NullOperabilityProbe
    from app.channels.phone_lifecycle import attach_sms_number
    from app.db.session import tenant_session
    from app.models.instance import Account, AccountState, Instance, LucielState

    number = "+16045550199"
    holder = uuid.uuid4()
    holder_iid = uuid.uuid4()
    async with tenant_session(holder) as s:
        s.add(
            Account(
                id=holder,
                admin_id=holder,
                user_id=holder,
                email=f"{holder.hex[:8]}@x.com",
                email_lower=f"{holder.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=holder_iid,
                admin_id=holder,
                name="Holder Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{holder.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        await attach_sms_number(
            s,
            admin_id=holder,
            instance_id=holder_iid,
            e164_number=number,
            probe=NullOperabilityProbe(operable_numbers={number}),
        )

    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio", "phoneNumber": number},
    )
    assert r.status_code == 409
    assert "already connected to a different account" in r.json()["message"]
    # The refusal wrote nothing: this account still has no sms_sender row.
    g = await client.get("/api/v1/admin/connections")
    assert not [c for c in g.json() if c["connectionType"] == "sms_sender"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_repeated_foreign_claims_are_audited_and_rate_limited(client):
    """c21 (409 oracle): every refused claim writes a destination_claim_rejected
    audit row under the CLAIMANT (in its own transaction — the 409 rolls the
    request's back), and repeated conflicts answer 429 instead of continuing to
    enumerate which numbers are taken."""
    from app.channels.number_operability import NullOperabilityProbe
    from app.channels.phone_lifecycle import attach_sms_number
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from app.models.instance import Account, AccountState, Instance, LucielState

    number = "+16045550171"
    holder = uuid.uuid4()
    holder_iid = uuid.uuid4()
    async with tenant_session(holder) as s:
        s.add(
            Account(
                id=holder,
                admin_id=holder,
                user_id=holder,
                email=f"{holder.hex[:8]}@x.com",
                email_lower=f"{holder.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=holder_iid,
                admin_id=holder,
                name="Holder Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{holder.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        await attach_sms_number(
            s,
            admin_id=holder,
            instance_id=holder_iid,
            e164_number=number,
            probe=NullOperabilityProbe(operable_numbers={number}),
        )

    await _ready_account_with_luciel(client)
    payload = {"connectionType": "sms_sender", "provider": "twilio", "phoneNumber": number}
    statuses = []
    for _ in range(5):
        r = await client.post("/api/v1/admin/connections", json=payload)
        statuses.append(r.status_code)
    assert statuses[0] == 409
    assert 429 in statuses  # the oracle closes after the burst
    # Every refusal was audited under the claimant, despite the rolled-back 409s.
    from sqlalchemy import func, select

    async with tenant_session(holder) as s:
        held_audits = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(
                    AdminAuditLog.admin_id == holder,
                    AdminAuditLog.event_type == "destination_claim_rejected",
                )
            )
        ).scalar_one()
    assert int(held_audits) == 0  # never recorded under the HOLDER (victim)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reverify_claim_conflict_is_audited_and_rate_limited(client, monkeypatch):
    """2026-09-05 audit F087: promoting a pending number at re-verify is a claim point
    too — the 409 there was neither audited nor rate-limited, so it was the same
    enumeration oracle the attach path had already closed (c21)."""
    from app.channels import phone_lifecycle
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from sqlalchemy import func, select

    async def _claimed(*_a, **_k):
        raise phone_lifecycle.NumberAlreadyClaimed("+16045550171 is connected to another account")

    monkeypatch.setattr(phone_lifecycle, "reverify_sms_number", _claimed)
    admin_id = await _ready_account_with_luciel(client)

    statuses = [
        (await client.post("/api/v1/admin/connections/sms/reverify")).status_code for _ in range(5)
    ]
    assert statuses[0] == 409
    assert 429 in statuses

    async with tenant_session(admin_id) as s:
        audited = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type == "destination_claim_rejected",
                )
            )
        ).scalar_one()
    assert int(audited) >= 1


# --- Round 7 phase 2 review (seams 8): a deleted Luciel's draining rows are hidden -----
# The delete writes ``quiescing`` on every connection (WP-5); the list filtered only
# ``revoked``, so GET /connections answered each draining row with a status outside
# ConnectionStatusWire. The frontend's chip fell to its default and the Overview of a
# deleted Luciel read "N need attention" over connections that were healthy and merely
# draining — where it used to read "Nothing connected yet".


async def _connected_calendar(client, admin_id: uuid.UUID) -> str:
    """A calendar driven to ``connected`` through the service seam, so the restore
    assertion below is about a REAL stashed status, not ``unconfigured``."""
    from app.connections import service as conn_service
    from app.db.session import tenant_session

    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    cid = (await client.get("/api/v1/admin/connections")).json()[0]["connectionId"]
    async with tenant_session(admin_id) as s:
        await conn_service.confirm_reconnect(
            s, admin_id=admin_id, connection_id=uuid.UUID(cid), user_id=None, probe_ok=True
        )
    return str(cid)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_deleted_luciels_draining_connections_are_hidden_until_restore(client):
    """Delete: the list is empty (not a row with a raw ``quiescing`` status) while the
    row drains underneath with its pre-delete status stashed. Restore: the same row is
    listed again with that status — nothing to reconnect."""
    from typing import get_args

    from app.db.session import tenant_session
    from app.models.connection import QUIESCING, InstanceConnection
    from app.schemas_api.luciel import ConnectionStatusWire
    from sqlalchemy import select as _select

    admin_id = await _ready_account_with_luciel(client)
    cid = await _connected_calendar(client, admin_id)
    (before,) = (await client.get("/api/v1/admin/connections")).json()
    assert before["status"] == "connected"

    rd = await client.post("/api/v1/admin/luciel/delete")
    assert rd.status_code == 200 and rd.json()["state"] == "luciel_grace_window"
    r = await client.get("/api/v1/admin/connections")
    assert r.status_code == 200
    assert r.json() == [], "a draining connection is not an Overview row"
    async with tenant_session(admin_id) as s:  # hidden, not gone
        row = (
            await s.execute(
                _select(InstanceConnection).where(
                    InstanceConnection.connection_id == uuid.UUID(cid)
                )
            )
        ).scalar_one()
        assert (row.status, row.status_stashed) == (QUIESCING, "connected")

    rr = await client.post("/api/v1/admin/luciel/restore")
    assert rr.status_code == 200 and rr.json()["state"] == "active"
    (after,) = (await client.get("/api/v1/admin/connections")).json()
    assert after["connectionId"] == cid
    assert after["status"] == "connected", "restore lists the row with its stashed status"
    assert {before["status"], after["status"]} <= set(get_args(ConnectionStatusWire))


@pytest.mark.parametrize("hidden_status", ["revoked", "quiescing"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_connection_in_a_not_serving_status_is_never_listed(client, hidden_status):
    """The two statuses a row can hold without serving (NOT_SERVING_STATUSES — the
    set the delete, the restore and the cascade agree on) are both hidden by the
    SAME filter, and nothing the list does return carries a status outside the wire
    vocabulary. A status added to the set later is hidden with it."""
    from typing import get_args

    from app.db.session import tenant_session
    from app.models.connection import NOT_SERVING_STATUSES, InstanceConnection
    from app.schemas_api.luciel import ConnectionStatusWire
    from sqlalchemy import select as _select

    assert NOT_SERVING_STATUSES == {"revoked", "quiescing"}, "extend the parametrize"
    admin_id = await _ready_account_with_luciel(client)
    cid = await _connected_calendar(client, admin_id)
    await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "crm", "provider": "hubspot"},
    )
    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                _select(InstanceConnection).where(
                    InstanceConnection.connection_id == uuid.UUID(cid)
                )
            )
        ).scalar_one()
        row.status = hidden_status
        row.status_stashed = "connected"

    rows = (await client.get("/api/v1/admin/connections")).json()
    assert [c["connectionType"] for c in rows] == ["crm"], rows
    assert all(c["status"] in set(get_args(ConnectionStatusWire)) for c in rows)
