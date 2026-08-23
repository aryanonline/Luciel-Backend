"""Twilio designate picker + credential-form swap completion (audit round 3, C9).

The platform already authenticates with the tenant's Twilio credentials (webhook
auto-config) yet the owner typed their E.164 by hand — the picker lists the
account's numbers instead, and ``numbers: null`` honestly signals "couldn't
list" so the UI falls back to manual entry. The swap half: ``swap`` stages
``pending_provider`` for proven-before-cutover, but the credential-form path
never completed or cleared it — fields now validate against the STAGED provider
and the marker never outlives the submit.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.tenant_twilio import MOCK_TENANT_NUMBERS

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"pick-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


async def _connect_sms(client) -> str:
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio"},
    )
    assert r.status_code in (200, 201), r.text
    rows = (await client.get("/api/v1/admin/connections")).json()
    cid = next(row["connectionId"] for row in rows if row["connectionType"] == "sms_sender")
    return str(cid)


async def _submit_credentials(client, cid: str, *, sid_digit: str = "0") -> None:
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/credentials",
        json={"fields": {"accountSid": "AC" + sid_digit * 32, "authToken": "tok"}},
    )
    assert r.status_code == 200, r.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_numbers_endpoint_lists_the_account_numbers(client):
    await _ready_account_with_luciel(client)
    cid = await _connect_sms(client)
    await _submit_credentials(client, cid)
    r = await client.get(f"/api/v1/admin/connections/{cid}/twilio/numbers")
    assert r.status_code == 200
    assert r.json()["numbers"] == [
        {"phoneNumber": n.phone_number, "friendlyName": n.friendly_name}
        for n in MOCK_TENANT_NUMBERS
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_numbers_endpoint_is_null_without_usable_credentials(client):
    """No stored credential → numbers: null (fall back to manual entry, never 500)."""
    await _ready_account_with_luciel(client)
    cid = await _connect_sms(client)
    r = await client.get(f"/api/v1/admin/connections/{cid}/twilio/numbers")
    assert r.status_code == 200
    assert r.json()["numbers"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_credential_submit_clears_a_staged_swap_marker(client):
    """A /swap on a credential-form row stages pending_provider; the submit that
    completes it must clear the marker — the stale stage previously lived forever."""
    await _ready_account_with_luciel(client)
    cid = await _connect_sms(client)
    await _submit_credentials(client, cid)
    r = await client.post(f"/api/v1/admin/connections/{cid}/swap", json={"provider": "twilio"})
    assert r.status_code == 200, r.text
    await _submit_credentials(client, cid, sid_digit="1")
    rows = (await client.get("/api/v1/admin/connections")).json()
    row = next(x for x in rows if str(x["connectionId"]) == cid)
    cfg = row.get("nonSecretConfig") or {}
    assert cfg.get("pending_provider") is None
    assert cfg.get("accountSid") == "AC" + "1" * 32
    assert row["status"] in ("unconfigured", "connected")  # needs-number honesty intact
