"""Durable A2P 10DLC acknowledgment + honest re-verify status (Legal §A2/§A6, Arch §3.1.4).

The acknowledgment gate existed only in client state, so it evaporated on reload and left
no record that the Admin had accepted the carrier/sender-of-record obligations. The
timestamp is now stamped server-side on the SMS channel the first time SMS is enabled —
reaching that mutation IS the acknowledgment — which also makes it auditable.

Also asserts the ``POST /connections/sms/reverify`` response shape the dashboard renders:
``status`` plus a ``statusDetail`` that explains WHY the number is still pending. (The
underlying transitions are covered at the service layer in
tests/integration/test_number_operability.py.)
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

PATH = "/api/v1/admin/luciel/channels"
NUMBER = "+14155550188"


async def _signup_verify_login(client) -> None:
    from app.auth import access

    email = f"a2p-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})


async def _create(client) -> None:
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )


def _payload(*, sms_enabled: bool, ack: str | None = None) -> dict:
    sms: dict = {"id": "sms", "enabled": sms_enabled}
    if ack is not None:
        sms["smsComplianceAcknowledgedAt"] = ack
    return {"channels": [{"id": "widget", "enabled": True}, sms]}


def _sms(response) -> dict:
    return next(c for c in response.json()["channels"] if c["id"] == "sms")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_enabling_sms_stamps_a_durable_acknowledgment(client):
    await _signup_verify_login(client)
    await _create(client)

    # Disabled SMS is not an acknowledgment — nothing to record yet.
    r0 = await client.put(PATH, json=_payload(sms_enabled=False))
    assert r0.status_code == 200
    assert _sms(r0).get("smsComplianceAcknowledgedAt") is None

    r1 = await client.put(PATH, json=_payload(sms_enabled=True))
    assert r1.status_code == 200
    acked_at = _sms(r1)["smsComplianceAcknowledgedAt"]
    assert acked_at

    # Durable across reads, so the dashboard gate survives a reload.
    r2 = await client.get("/api/v1/admin/luciel")
    assert _sms(r2)["smsComplianceAcknowledgedAt"] == acked_at

    # A later disable does NOT erase the fact that the obligations were accepted.
    r3 = await client.put(PATH, json=_payload(sms_enabled=False))
    assert _sms(r3)["smsComplianceAcknowledgedAt"] == acked_at


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_acknowledgment_instant_is_server_owned_not_client_supplied(client):
    """The client may echo the field back (no 422) but can never set or backdate it."""
    await _signup_verify_login(client)
    await _create(client)

    forged = "2001-01-01T00:00:00Z"
    r1 = await client.put(PATH, json=_payload(sms_enabled=True, ack=forged))
    assert r1.status_code == 200
    acked_at = _sms(r1)["smsComplianceAcknowledgedAt"]
    assert acked_at != forged

    # Re-enabling with a different forged value cannot move the recorded instant either.
    r2 = await client.put(PATH, json=_payload(sms_enabled=True, ack="2030-01-01T00:00:00Z"))
    assert _sms(r2)["smsComplianceAcknowledgedAt"] == acked_at


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reverify_explains_why_the_number_is_still_pending(client):
    await _signup_verify_login(client)
    await _create(client)
    attach = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio", "phoneNumber": NUMBER},
    )
    assert attach.status_code == 200

    r = await client.post("/api/v1/admin/connections/sms/reverify")
    assert r.status_code == 200
    body = r.json()
    # Never "connected" straight out of the probe, and never a bare status with no reason:
    # the dashboard renders statusDetail as the explanation of the pending state.
    assert body["status"] != "connected"
    assert body["statusDetail"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reverify_without_an_attached_number_is_not_found(client):
    await _signup_verify_login(client)
    await _create(client)
    r = await client.post("/api/v1/admin/connections/sms/reverify")
    assert r.status_code == 404
