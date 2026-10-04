"""The OAuth callback's two cross-tenant branches (round 7 WP-3c items 12 + 13).

Item 12 — a mailbox another tenant holds refused with a bare 409: unaudited and
unlimited, so the callback was an enumeration oracle for which mailboxes are
connected (the SMS attach closed the same hole under c21). Every refusal now
writes ``destination_claim_rejected`` under the CLAIMANT (own transaction) and
charges the ``destination_claim`` bucket; the burst closes with a 429. The
browser-redirect callback still lands on ``exchange_failed`` (the 429 class is an
``OAuthExchangeRejected``), so nothing about the holder leaks into a URL.

Item 13 — owner ruling 2026-09-14: a CRM portal (HubSpot ``hubId`` / Salesforce
``orgId``) two accounts connect is ALLOWED. A franchise or an agency legitimately
runs two Luciels into one CRM, so this is informational: the row carries a note,
the platform audits ``crm_portal_shared`` (counts and a suffix — never the other
account), and the connection completes exactly as an unshared one does.

The Outlook helpers are the mailbox suite's (same Microsoft fake), imported by name.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from app.config import Settings
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from sqlalchemy import select

from tests.contract.test_outlook_mailbox_connect import (
    MAILBOX,
    _configure_microsoft,
    _fake_microsoft_transport,
    _provision_ses,
    _ready_account_with_luciel,
    _row,
    _swap_to_outlook,
)

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"  # noqa: S105 — an endpoint
PORTAL = "424242"


async def _audits(admin_id: uuid.UUID, event_type: str) -> list[dict]:
    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin_id, AdminAuditLog.event_type == event_type
                )
            )
        ).scalars()
        return [dict(e.event_payload or {}) for e in rows]


# --------------------------------------------------------------------------- #
# item 12 — the mailbox bind is a claim point: audited + rate-limited
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_repeated_mailbox_claims_are_audited_and_rate_limited(client, monkeypatch):
    # Tenant A holds the mailbox.
    holder = await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch)
    conn_a = await _provision_ses(client)
    state_a = await _swap_to_outlook(client, conn_a)
    ok = await client.post(
        f"/api/v1/admin/connections/{conn_a}/oauth-callback",
        json={"code": "code-a", "state": state_a},
    )
    assert ok.status_code == 200, ok.text

    # Tenant B keeps trying the SAME mailbox (each attempt is a fresh swap: the
    # state is single-use, and every failed callback resolves the swap back to SES).
    await client.post("/api/v1/auth/logout")
    claimant = await _ready_account_with_luciel(client)
    conn_b = await _provision_ses(client)
    statuses = []
    for attempt in range(5):
        state_b = await _swap_to_outlook(client, conn_b)
        r = await client.post(
            f"/api/v1/admin/connections/{conn_b}/oauth-callback",
            json={"code": f"code-b-{attempt}", "state": state_b},
        )
        statuses.append(r.status_code)
        assert (await _row(claimant)).provider == "ses"  # the working sender, every time
    assert statuses[0] == 409
    assert 429 in statuses  # the oracle closes after the burst
    assert 200 not in statuses

    rejections = await _audits(claimant, "destination_claim_rejected")
    assert len(rejections) == 5
    assert {p["connection_type"] for p in rejections} == {"email_sender"}
    assert {p["destination_suffix"] for p in rejections} == {MAILBOX[-4:]}
    assert not any(MAILBOX in str(p) for p in rejections)  # never the full address
    assert await _audits(holder, "destination_claim_rejected") == []  # never under the victim
    assert (await _row(holder)).non_secret_config["destination"] == MAILBOX  # intact


# --------------------------------------------------------------------------- #
# item 13 — a shared CRM portal is informational, never a refusal
# --------------------------------------------------------------------------- #


def _configure_hubspot(monkeypatch) -> None:
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        hubspot_client_id="hs-cid",
        hubspot_client_secret="hs-csecret",  # noqa: S106 — test fixture
        hubspot_redirect_uri="https://api.example.com/api/v1/connections/hubspot/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_hubspot(monkeypatch, *, hub_id: str) -> None:
    """Token endpoint fake + a verifier that names the portal (the real verifier's
    shape, minus HubSpot's token-info call — what the callback does with ``hubId``
    is the thing under test)."""
    import app.api.v1.admin.connections as connections_api

    class _Transport:
        async def post(self, url, *, data=None, headers=None):
            assert url == HUBSPOT_TOKEN_URL
            return httpx.Response(
                200,
                json={"access_token": "hs-tok", "refresh_token": "hs-ref", "expires_in": 1800},
                request=httpx.Request("POST", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _verified(*, material, transport):
        assert material.get("access_token") == "hs-tok"
        return {
            "hubDomain": "acme.hubspot.com",
            "hubId": hub_id,
            "user": "ops@acme.example",
            "grantedScopes": "crm.objects.contacts.read crm.objects.contacts.write",
            "connectedAs": f"acme.hubspot.com (portal {hub_id})",
        }

    monkeypatch.setattr(connections_api, "oauth_transport", _Transport)
    monkeypatch.setitem(connections_api.POST_EXCHANGE_VERIFIERS, "hubspot", _verified)


async def _connect_hubspot(client) -> tuple[str, httpx.Response]:
    from urllib.parse import parse_qs, urlparse

    rs = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "crm", "provider": "hubspot"}
    )
    assert rs.status_code == 200, rs.text
    state = parse_qs(urlparse(rs.json()["authorizeUrl"]).query)["state"][0]
    cid = next(
        c["connectionId"]
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "crm"
    )
    cb = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback",
        json={"code": f"hs-{uuid.uuid4().hex[:6]}", "state": state},
    )
    return cid, cb


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_crm_portal_another_account_holds_connects_with_an_informational_note(
    client, monkeypatch
):
    # Tenant A already pushes into portal 424242.
    holder = await _ready_account_with_luciel(client)
    async with tenant_session(holder) as s:
        from app.models.instance import Instance

        holder_iid = (
            await s.execute(select(Instance.id).where(Instance.admin_id == holder))
        ).scalar_one()
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=holder,
                instance_id=holder_iid,
                connection_type="crm",
                auth_class="oauth_token",
                provider="hubspot",
                status="connected",
                non_secret_config={"hubDomain": "acme.hubspot.com", "hubId": PORTAL},
            )
        )

    # Tenant B connects the SAME portal.
    await client.post("/api/v1/auth/logout")
    admin_b = await _ready_account_with_luciel(client)
    _configure_hubspot(monkeypatch)
    _fake_hubspot(monkeypatch, hub_id=PORTAL)
    cid, cb = await _connect_hubspot(client)
    assert cb.status_code == 200, cb.text  # ALLOWED — the owner ruling
    body = cb.json()
    assert body["status"] == "connected"
    assert body["nonSecretConfig"]["hubId"] == PORTAL
    assert "also connected to another VantageMind account" in (body["statusDetail"] or "")

    shared = await _audits(admin_b, "crm_portal_shared")
    assert len(shared) == 1
    assert shared[0] == {
        "provider": "hubspot",
        "portal_key": "hubId",
        "portal_id_suffix": PORTAL[-4:],
        "other_account_count": 1,
    }
    assert str(holder) not in str(shared[0])  # never WHICH account
    assert await _audits(holder, "crm_portal_shared") == []  # nothing written under A
    # And nothing about it was a claim refusal.
    assert await _audits(admin_b, "destination_claim_rejected") == []

    # The note survives the row's listing and is not duplicated by a re-read.
    listed = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionId"] == cid
    )
    assert listed["statusDetail"].count("also connected to another VantageMind account") == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_portal_nobody_else_holds_connects_with_no_note_and_no_event(client, monkeypatch):
    admin = await _ready_account_with_luciel(client)
    _configure_hubspot(monkeypatch)
    _fake_hubspot(monkeypatch, hub_id="777777")
    _cid, cb = await _connect_hubspot(client)
    assert cb.status_code == 200, cb.text
    assert cb.json()["status"] == "connected"
    assert cb.json()["statusDetail"] is None
    assert await _audits(admin, "crm_portal_shared") == []
