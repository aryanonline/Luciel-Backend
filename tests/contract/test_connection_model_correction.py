"""The §3.8 connection model as the product actually needs it (connection_model_correction).

§3.8 shipped as a *generic provider picker*: it could create a connection, but not one
wired to the CUSTOMER'S OWN account. Four defects followed from that, and each one is a
thing a paying owner would hit on day one:

  A. the providers endpoint filtered on ``connection_type`` while the UI sent
     ``connectionType``, so FastAPI dropped the unknown param and returned the WHOLE
     catalog — a CRM picker offering Google Calendar,
  B. SMS sent through the PLATFORM's Twilio, which §3.1.4 forbids outright (VantageMind is
     never in the telephony billing path),
  C. WhatsApp and Instagram were two connections of the same single-active type, so
     enabling the second silently DISCONNECTED the first.

These tests are the regression floor for all three. The registry table they read from is
pinned separately in tests/unit/test_connection_provider_registry.py.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"cmc-{uuid.uuid4().hex[:8]}@x.com"
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


# --------------------------------------------------------------------------- #
# ITEM A — the filter the frontend actually sends, and the provider it picked
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_providers_accepts_the_camelcase_filter_the_frontend_sends(client):
    """``?connectionType=crm`` must filter, not be ignored.

    FastAPI drops query params it does not declare, so the UI's camelCase filter used to
    return every connection type — which is how a CRM picker ended up offering calendar
    providers. Both spellings are accepted; snake_case stays for API clients."""
    await _ready_account_with_luciel(client)

    camel = await client.get("/api/v1/admin/connections/providers?connectionType=crm")
    assert camel.status_code == 200, camel.text
    assert [row["connectionType"] for row in camel.json()] == ["crm"]

    snake = await client.get("/api/v1/admin/connections/providers?connection_type=crm")
    assert snake.status_code == 200
    assert snake.json() == camel.json()

    # No filter still means "the whole catalog", which is what the overview page wants.
    every = await client.get("/api/v1/admin/connections/providers")
    assert len(every.json()) > 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_salesforce_crm_availability_follows_the_oauth_app(client, monkeypatch):
    """Phase 6.5 (owner decision 2026-08-23): Salesforce lead-push is REAL, so
    its availability now tracks only whether the platform's Salesforce OAuth app
    is provisioned — exactly like every other OAuth provider, with no
    unavailable_for scoping left on the crm surface."""
    from app.config import Settings
    from app.connections import registry as registry_module

    await _ready_account_with_luciel(client)
    crm = await client.get("/api/v1/admin/connections/providers?connectionType=crm")
    options = {o["provider"]: o for o in crm.json()[0]["providers"]}
    # This environment holds no Salesforce OAuth app → honestly not offerable yet.
    assert options["salesforce"]["configured"] is False
    # The custom-webhook CRM path is a credential form — always offerable.
    assert options["custom_webhook"]["configured"] is True

    # Provision the app → salesforce becomes a live connect choice.
    settings = Settings(
        salesforce_consumer_key="sf-cid",
        salesforce_consumer_secret="sf-secret",  # noqa: S106 — test fixture
        salesforce_redirect_uri="https://api.vantagemind.ai/sf-callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    crm2 = await client.get("/api/v1/admin/connections/providers?connectionType=crm")
    options2 = {o["provider"]: o for o in crm2.json()[0]["providers"]}
    assert options2["salesforce"]["configured"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_chosen_provider_is_what_the_connect_flow_starts(client, monkeypatch):
    """Picking Salesforce must start Salesforce — provider choice drives the flow.

    The picker is pointless if connect always runs the first vendor: the authorize URL and
    the stored provider both have to follow the selection (Decision #6)."""
    from app.config import Settings
    from app.connections import registry as registry_module

    settings = Settings(
        salesforce_consumer_key="sf-cid",
        salesforce_consumer_secret="sf-secret",  # noqa: S106 — test fixture, not a real secret
        salesforce_redirect_uri="https://api.vantagemind.ai/sf-callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    await _ready_account_with_luciel(client)

    r = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "crm", "provider": "salesforce"}
    )
    assert r.status_code == 200, r.text
    assert "salesforce.com" in r.json()["authorizeUrl"]

    row = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "crm"
    )
    assert row["provider"] == "salesforce"

    # A different choice on the same type re-points the flow rather than sticking.
    swapped = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "crm", "provider": "custom_webhook"}
    )
    assert swapped.json()["requiresClientForm"] is True
    assert swapped.json().get("authorizeUrl") is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unconfigured_oauth_providers_report_configured_false(client):
    """Honest-disabled (§3.8 correction): the platform has no OAuth app for Google
    Calendar, Calendly or Meta yet, so the UI must be TOLD that and render "Not available
    yet" — flipping the flag to true would fail the owner at the authorize step instead.

    BYO-credential providers are the opposite case: they need nothing from the platform,
    so they are always available."""
    await _ready_account_with_luciel(client)
    r = await client.get("/api/v1/admin/connections/providers")
    by_provider = {p["provider"]: p for row in r.json() for p in row["providers"]}

    for provider in ("google_calendar", "calendly", "meta", "instagram"):
        assert by_provider[provider]["configured"] is False, provider
    # Round 6 WP-H (F062): the live record connector's lookup effect layer is built
    # (LiveRecordBackend through the pinned sandbox transport), so its credential form
    # is offered like every other BYO-credential provider.
    for provider in ("twilio", "custom_webhook", "csv", "live_connector"):
        assert by_provider[provider]["configured"] is True, provider


# --------------------------------------------------------------------------- #
# ITEM B — BYO Twilio (§3.1.4): the customer's own account, not the platform's
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_sms_sender_offers_a_twilio_credential_form(client):
    await _ready_account_with_luciel(client)
    r = await client.get("/api/v1/admin/connections/providers?connectionType=sms_sender")
    providers = r.json()[0]["providers"]
    assert [p["provider"] for p in providers] == ["twilio"]

    twilio = providers[0]
    # OAuth-primary since C10 (owner decision 2026-08-23) — the credential form
    # below is the explicit "use API keys instead" fallback and stays served.
    assert twilio["authKind"] == "oauth"
    fields = {f["name"]: f for f in twilio["credentialFields"]}
    assert set(fields) == {"accountSid", "authToken", "apiKeySid", "apiKeySecret"}
    # The Account SID is an identifier and is stored on the row; the rest are secrets and
    # must be marked so the UI masks them and we route them to the SecretStore (§3.8.3).
    assert fields["accountSid"]["secret"] is False
    assert fields["authToken"]["secret"] is True
    assert fields["apiKeySecret"]["secret"] is True


async def _connect_twilio(client, fields: dict[str, str]) -> tuple[str, dict]:
    start = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "sms_sender", "provider": "twilio"}
    )
    assert start.status_code == 200, start.text
    assert start.json()["requiresClientForm"] is True
    cid = next(
        c["connectionId"]
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "sms_sender"
    )
    r = await client.post(f"/api/v1/admin/connections/{cid}/credentials", json={"fields": fields})
    assert r.status_code == 200, r.text
    return cid, r.json()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connecting_twilio_puts_the_token_in_the_secret_store_and_never_in_postgres(
    client,
):
    """§3.8.3 is the whole point of the secret_ref indirection: the customer's Auth Token
    is credential material, so Postgres may hold the pointer and the Account SID, never
    the token itself."""
    from app.connections.secrets import get_secret_store
    from app.db.session import tenant_session
    from app.models.connection import InstanceConnection
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    cid, body = await _connect_twilio(
        client, {"accountSid": "AC" + "1" * 30, "authToken": "tok-super-secret"}
    )

    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == uuid.UUID(cid))
            )
        ).scalar_one()
        secret_ref = row.secret_ref
        config = dict(row.non_secret_config or {})

    assert secret_ref
    assert config["accountSid"] == "AC" + "1" * 30
    assert "tok-super-secret" not in str(config)
    assert await get_secret_store().get(secret_ref) == {"authToken": "tok-super-secret"}

    # Nor does the token come back out on the wire (§3.8.3 — the response has no field
    # that could carry it).
    assert "tok-super-secret" not in str(body)
    assert body["nonSecretConfig"]["accountSid"] == "AC" + "1" * 30


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_connected_twilio_without_a_number_says_action_needed(client):
    """An account with no designated number cannot text anyone, so the row must not claim
    to be connected — the owner has one more step and deserves to be told which."""
    await _ready_account_with_luciel(client)
    _cid, body = await _connect_twilio(
        client, {"accountSid": "AC" + "2" * 30, "authToken": "tok-2"}
    )
    assert body["status"] == "unconfigured"
    assert "Action needed: add your number" in body["statusDetail"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_designating_the_byo_number_keeps_the_credential_and_runs_the_a2p_gate(client):
    """Designating the number reuses the §3.1.6 attach path, so the operability + A2P gate
    still runs — a BYO number is not a way around carrier registration. (In mock the probe
    calls every number non-operable, which is the honest hermetic default.)

    The part this item adds: the designation must not cost the owner the credential they
    just submitted. Both live on the one sms_sender row."""
    await _ready_account_with_luciel(client)
    await _connect_twilio(client, {"accountSid": "AC" + "3" * 30, "authToken": "tok-3"})

    number = "+15550000123"
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio", "phoneNumber": number},
    )
    assert r.status_code == 200, r.text
    row = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "sms_sender"
    )
    # c21 anti-squatting: a number the probe could not confirm is NOT the routable
    # claim — it waits under pending_destination until a confirmed re-verify.
    assert row["nonSecretConfig"]["pending_destination"] == number
    assert "destination" not in row["nonSecretConfig"]
    # The credential survived the designation — attaching a number must not re-open the
    # connect form the owner already completed.
    assert row["nonSecretConfig"]["accountSid"] == "AC" + "3" * 30
    assert row["status"] == "not_operable_hosting_required"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_outbound_sms_sends_through_the_tenants_own_twilio(client, monkeypatch):
    """§3.1.4, the reason this item exists: carrier cost and sender reputation belong to
    the customer's Twilio account. The sender is built from THEIR stored credential, not
    from the platform settings."""
    from app.channels import tenant_twilio
    from app.db.session import tenant_session

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)
    await _connect_twilio(client, {"accountSid": "AC" + "4" * 30, "authToken": "tok-4"})
    # Attach with a CONFIRMED-operable probe (service layer): outbound needs the
    # routable claim, which mock's never-operable probe deliberately withholds (c21).
    from app.channels.number_operability import NullOperabilityProbe
    from app.channels.phone_lifecycle import attach_sms_number

    async with tenant_session(admin_id) as s:
        await attach_sms_number(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            e164_number="+15550000456",
            probe=NullOperabilityProbe(operable_numbers={"+15550000456"}),
        )

    async with tenant_session(admin_id) as s:
        creds = await tenant_twilio.load_tenant_credentials(s, admin_id, instance_id)
    assert creds is not None
    assert creds.account_sid == "AC" + "4" * 30
    assert creds.auth_token == "tok-4"  # noqa: S105 — test fixture, not a real secret
    assert creds.usable

    sent: list[tuple[str, str, str]] = []

    class _RecordingSender:
        async def send_sms(self, from_number: str, to: str, body: str) -> str:
            sent.append((from_number, to, body))
            return "sid-1"

    async def _build(session, a, i, **kw):
        assert (a, i) == (admin_id, instance_id)
        return _RecordingSender()

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _build)

    from app.channels import outbound

    async with tenant_session(admin_id) as s:
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            channel="sms",
            to="+15551110000",
            body="hi",
        )
    assert result.delivered is True
    assert sent == [("+15550000456", "+15551110000", "hi")]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnecting_twilio_destroys_the_stored_credential(client):
    """Handing the integration back means the platform can no longer send as them."""
    from app.connections.secrets import get_secret_store
    from app.db.session import tenant_session
    from app.models.connection import InstanceConnection
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    cid, _ = await _connect_twilio(client, {"accountSid": "AC" + "5" * 30, "authToken": "tok-5"})
    async with tenant_session(admin_id) as s:
        ref = (
            await s.execute(
                select(InstanceConnection.secret_ref).where(
                    InstanceConnection.connection_id == uuid.UUID(cid)
                )
            )
        ).scalar_one()
    assert await get_secret_store().get(ref) is not None

    r = await client.post(f"/api/v1/admin/connections/{cid}/disconnect")
    assert r.status_code == 200, r.text
    assert r.json()["secretDeleted"] is True
    assert await get_secret_store().get(ref) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_twilio_credential_without_an_account_sid_is_refused(client):
    """An API Key SID alone cannot authenticate a Twilio REST call, and it cannot verify an
    inbound signature either. Refusing at the form is honest; accepting would produce a
    connection that looks healthy and silently fails to send."""
    await _ready_account_with_luciel(client)
    await client.post(
        "/api/v1/admin/connections", json={"connectionType": "sms_sender", "provider": "twilio"}
    )
    cid = next(
        c["connectionId"]
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "sms_sender"
    )
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/credentials",
        json={"fields": {"accountSid": "", "apiKeySid": "SK" + "1" * 30}},
    )
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# ITEM C — ONE Meta grant for WhatsApp + Instagram + Messenger
# --------------------------------------------------------------------------- #
def _configure_meta(monkeypatch) -> None:
    """Point every settings read the connect flow makes at configured Meta apps.

    Dev has no Meta OAuth app (which is exactly why ``configured`` is false above), so the
    flow has to be handed one or connect-start degrades to ``requiresClientForm``. Instagram
    messaging is a SECOND app under Meta's use-case model, hence a second id/secret pair."""
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        meta_app_id="meta-cid",
        meta_app_secret="meta-csecret",  # noqa: S106 — test fixture, not a real secret
        meta_redirect_uri="https://api.vantagemind.ai/meta-callback",
        instagram_app_id="ig-cid",
        instagram_app_secret="ig-csecret",  # noqa: S106 — test fixture, not a real secret
        instagram_redirect_uri="https://api.vantagemind.ai/instagram-callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_meta_token_exchange(expected_code: str, monkeypatch) -> None:
    """A transport standing in for both Meta token servers.

    Both grants redeem the code to a SHORT-lived token and upgrade it with a second
    GET — Facebook via ``fb_exchange_token``, Instagram via ``ig_exchange_token`` —
    so the fake has to answer both verbs or neither callback path completes.
    """
    import app.api.v1.admin.connections as connections_api
    import httpx

    class _Transport:
        async def post(self, url, *, data=None, headers=None):
            assert data["code"] == expected_code
            body: dict[str, object] = {"access_token": "meta-tok", "expires_in": 5184000}
            if "instagram" in url:
                body = {"access_token": "ig-short-tok", "user_id": "17841400000000000"}
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        async def get(self, url, *, params=None, headers=None):
            if "/me" in url:
                # Round 6 WP-C: both grants are verified against /me before they flip.
                return httpx.Response(
                    200,
                    json={"id": "10001", "name": "Tester", "username": "acme"},
                    request=httpx.Request("GET", url),
                )
            if params["grant_type"] == "fb_exchange_token":
                assert params["client_id"] == "meta-cid"
                assert params["fb_exchange_token"] == "meta-tok"
                return httpx.Response(
                    200,
                    json={"access_token": "meta-tok-long", "expires_in": 5184000},
                    request=httpx.Request("GET", url),
                )
            assert params["grant_type"] == "ig_exchange_token"
            assert params["access_token"] == "ig-short-tok"
            return httpx.Response(
                200,
                json={"access_token": "ig-long-tok", "expires_in": 5184000},
                request=httpx.Request("GET", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(connections_api, "oauth_transport", _Transport)


async def _connect_oauth(client, monkeypatch, connection_type: str, provider: str) -> str:
    """Drive one OAuth channel connection to `connected` and return its id."""
    from urllib.parse import parse_qs, urlparse

    _configure_meta(monkeypatch)
    code = f"{provider}-{uuid.uuid4().hex[:6]}"
    _fake_meta_token_exchange(code, monkeypatch)
    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": connection_type, "provider": provider},
    )
    assert rs.status_code == 200, rs.text
    state = parse_qs(urlparse(rs.json()["authorizeUrl"]).query)["state"][0]
    cid = next(
        c["connectionId"]
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == connection_type
    )
    cb = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback", json={"code": code, "state": state}
    )
    assert cb.status_code == 200, cb.text
    return cid


async def _connected_meta(client, monkeypatch) -> str:
    return await _connect_oauth(client, monkeypatch, "channel_auth", "meta")


async def _connected_instagram(client, monkeypatch) -> str:
    return await _connect_oauth(client, monkeypatch, "instagram_auth", "instagram")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_one_meta_connection_binds_both_facebook_channels_without_evicting_each_other(
    client, monkeypatch
):
    """The defect this replaces: WhatsApp and Messenger were separate connections of the
    same single-active type, so connecting the second REVOKED the first — an owner who
    turned on Messenger silently lost WhatsApp. One Facebook grant, both destinations."""
    await _ready_account_with_luciel(client)
    cid = await _connected_meta(client, monkeypatch)

    bound = {}
    for channel, destination in (
        ("whatsapp", f"pnid-{uuid.uuid4().hex[:8]}"),
        ("messenger", f"pgid-{uuid.uuid4().hex[:8]}"),
    ):
        r = await client.put(
            f"/api/v1/admin/connections/{cid}/destination",
            json={"destination": destination, "channel": channel},
        )
        assert r.status_code == 200, r.text
        bound[channel] = destination
        # Every channel bound SO FAR is still bound: the later binding added, not replaced.
        assert r.json()["nonSecretConfig"]["destinations"] == bound

    # Still exactly ONE connection of this type, still connected.
    rows = [
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "channel_auth"
    ]
    assert len(rows) == 1
    assert rows[0]["connectionId"] == cid
    assert rows[0]["status"] == "connected"
    assert rows[0]["provider"] == "meta"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_instagram_binds_on_its_own_grant_and_the_two_coexist(client, monkeypatch):
    """Instagram messaging moved to its own Meta app, so it is its own connection — but the
    original eviction defect must not come back through the side door: connecting Instagram
    has to leave the Facebook grant (WhatsApp + Messenger) alive and bound.

    The channel is also refused on the wrong grant. Binding an IG account under the Facebook
    connection would store a destination whose token lives on the other app, and the send
    would fail at Graph long after the owner was told it was connected."""
    await _ready_account_with_luciel(client)
    meta_cid = await _connected_meta(client, monkeypatch)
    wa = f"pnid-{uuid.uuid4().hex[:8]}"
    assert (
        await client.put(
            f"/api/v1/admin/connections/{meta_cid}/destination",
            json={"destination": wa, "channel": "whatsapp"},
        )
    ).status_code == 200

    ig = f"igid-{uuid.uuid4().hex[:8]}"
    assert (
        await client.put(
            f"/api/v1/admin/connections/{meta_cid}/destination",
            json={"destination": ig, "channel": "instagram"},
        )
    ).status_code == 422

    ig_cid = await _connected_instagram(client, monkeypatch)
    assert ig_cid != meta_cid
    r = await client.put(
        f"/api/v1/admin/connections/{ig_cid}/destination",
        json={"destination": ig, "channel": "instagram"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["nonSecretConfig"]["destinations"] == {"instagram": ig}

    listed = (await client.get("/api/v1/admin/connections")).json()
    by_type = {c["connectionType"]: c for c in listed}
    assert by_type["channel_auth"]["status"] == "connected"
    assert by_type["channel_auth"]["nonSecretConfig"]["destinations"] == {"whatsapp": wa}
    assert by_type["instagram_auth"]["provider"] == "instagram"
    assert by_type["instagram_auth"]["status"] == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_inbound_routing_resolves_each_meta_channel_by_its_own_destination(
    client, monkeypatch
):
    """Routing is pre-tenant: an inbound WhatsApp message carries a phone-number id and
    nothing else, so the SECURITY DEFINER resolver has to find the tenant from the
    per-channel map. If it only looked at the legacy scalar, per-channel binding would
    resolve to nobody and every inbound would be rejected."""
    from app.channels import routing
    from app.db.session import tenant_session

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    cid = await _connected_meta(client, monkeypatch)
    ig_cid = await _connected_instagram(client, monkeypatch)
    wa = f"pnid-{uuid.uuid4().hex[:8]}"
    page = f"pgid-{uuid.uuid4().hex[:8]}"
    ig = f"igid-{uuid.uuid4().hex[:8]}"
    for conn, channel, destination in (
        (cid, "whatsapp", wa),
        (cid, "messenger", page),
        (ig_cid, "instagram", ig),
    ):
        assert (
            await client.put(
                f"/api/v1/admin/connections/{conn}/destination",
                json={"destination": destination, "channel": channel},
            )
        ).status_code == 200

    async with tenant_session(admin_id) as s:
        assert await routing.owner_of_destination(s, "channel_auth", wa) == admin_id
        assert await routing.owner_of_destination(s, "channel_auth", page) == admin_id
        assert await routing.owner_of_destination(s, "instagram_auth", ig) == admin_id
        assert await routing.owner_of_destination(s, "channel_auth", "pnid-nobody") is None
        # The grants do not see each other's assets: a destination is owned by the app whose
        # token can actually send as it.
        assert await routing.owner_of_destination(s, "channel_auth", ig) is None
        assert await routing.owner_of_destination(s, "instagram_auth", wa) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_each_meta_channel_replies_from_its_own_sender_identity(client, monkeypatch):
    """A WhatsApp reply must go out from the WhatsApp number id and a Messenger reply from
    the Page id. Sharing one identity across channels would make the send fail at Graph."""
    from app.connections.service import resolve_meta_destination
    from app.db.session import tenant_session

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)
    cid = await _connected_meta(client, monkeypatch)
    wa = f"pnid-{uuid.uuid4().hex[:8]}"
    page = f"pgid-{uuid.uuid4().hex[:8]}"
    for channel, destination in (("whatsapp", wa), ("messenger", page)):
        await client.put(
            f"/api/v1/admin/connections/{cid}/destination",
            json={"destination": destination, "channel": channel},
        )

    async with tenant_session(admin_id) as s:
        assert await resolve_meta_destination(s, admin_id, instance_id, "whatsapp") == wa
        assert await resolve_meta_destination(s, admin_id, instance_id, "messenger") == page
        # Legacy combined id (pre-§3.1.2-split sessions) still resolves for old rows.
        assert await resolve_meta_destination(s, admin_id, instance_id, "instagram_messenger") == (
            page
        )
