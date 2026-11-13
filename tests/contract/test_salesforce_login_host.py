"""The connect route takes the Salesforce login host (round 7 WP-7).

``POST /api/v1/admin/connections`` gains an optional ``loginHost`` body field (no new
route — the registry stays as it was): ``sandbox`` sends the consent to
test.salesforce.com, records the choice on the row, and the callback redeems the code at
the same host; omitted means production; a choice the provider does not offer is a 422
before any row is written. ``GET /connections/providers`` advertises the choices.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = [pytest.mark.contract]


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"sfhost-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


def _configure_salesforce(monkeypatch) -> None:
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        salesforce_consumer_key="sf-cid",
        salesforce_consumer_secret="sf-secret",  # noqa: S106 — test fixture
        salesforce_redirect_uri="https://api.example.test/api/v1/connections/salesforce/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


async def _crm_row(client) -> dict:
    return next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionType"] == "crm"
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_providers_list_advertises_the_choice(client, monkeypatch):
    _configure_salesforce(monkeypatch)
    await _ready_account_with_luciel(client)
    crm = await client.get("/api/v1/admin/connections/providers?connectionType=crm")
    options = {o["provider"]: o for o in crm.json()[0]["providers"]}
    assert options["salesforce"]["loginHosts"] == ["production", "sandbox"]
    assert options["hubspot"]["loginHosts"] == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_sandbox_connect_consents_and_redeems_at_test_salesforce(client, monkeypatch):
    import app.api.v1.admin.connections as connections_api
    from app.connections import verify

    _configure_salesforce(monkeypatch)
    await _ready_account_with_luciel(client)

    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "crm", "provider": "salesforce", "loginHost": "sandbox"},
    )
    assert r.status_code == 200, r.text
    authorize_url = r.json()["authorizeUrl"]
    assert urlparse(authorize_url).netloc == "test.salesforce.com"

    posted: list[str] = []

    class _TokenServer:
        async def post(self, url, *, data=None, json=None, headers=None):
            posted.append(url)
            return httpx.Response(
                200,
                json={
                    "access_token": "sf-at",
                    "refresh_token": "sf-rt",
                    "instance_url": "https://acme--dev.sandbox.my.salesforce.com",
                },
                request=httpx.Request("POST", url),
            )

        async def get(self, url, *, params=None, headers=None):  # pragma: no cover
            raise AssertionError("the verifier is stubbed below")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _verified(*, material, transport):
        return {"orgId": "00Dsandbox", "connectedAs": "Salesforce · Acme (sandbox)"}

    monkeypatch.setattr(connections_api, "oauth_transport", lambda: _TokenServer())
    # (connections_api imports this same dict object.)
    monkeypatch.setitem(verify.POST_EXCHANGE_VERIFIERS, "salesforce", _verified)

    row = await _crm_row(client)
    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    rcb = await client.post(
        f"/api/v1/admin/connections/{row['connectionId']}/oauth-callback",
        json={"code": "sf-code", "state": state},
    )
    assert rcb.status_code == 200, rcb.text
    # The code is redeemed where the consent ran — a sandbox code is unknown to the
    # production host.
    assert [urlparse(u).netloc for u in posted] == ["test.salesforce.com"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_omitted_means_production(client, monkeypatch):
    _configure_salesforce(monkeypatch)
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/connections", json={"connectionType": "crm", "provider": "salesforce"}
    )
    assert r.status_code == 200, r.text
    assert urlparse(r.json()["authorizeUrl"]).netloc == "login.salesforce.com"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(
    "body",
    [
        {"connectionType": "crm", "provider": "salesforce", "loginHost": "staging"},
        {"connectionType": "crm", "provider": "hubspot", "loginHost": "sandbox"},
    ],
)
async def test_a_choice_the_provider_does_not_offer_is_refused_before_any_row(
    client, monkeypatch, body
):
    _configure_salesforce(monkeypatch)
    await _ready_account_with_luciel(client)
    r = await client.post("/api/v1/admin/connections", json=body)
    assert r.status_code == 422, r.text
    listed = (await client.get("/api/v1/admin/connections")).json()
    assert not any(c["connectionType"] == "crm" for c in listed)
