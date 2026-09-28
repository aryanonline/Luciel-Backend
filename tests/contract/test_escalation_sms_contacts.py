"""SMS escalation contacts are validated at save time and provable by a test text
(2026-09-05 audit F084).

Email contacts confirm themselves through a link; a phone number had no loop at all —
the first real hot lead was the test. Now a number must be E.164 to save, and the
owner can send themselves a test text through the tenant's own number (BYO), which
also proves the number can send. Rate-limited so it cannot become a texting tool.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import attach_sms_number
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

BYO_NUMBER = "+16045550100"
CONTACT = "+16045551234"


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"esc-sms-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    r = await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    assert r.status_code in (200, 201), r.text
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _instance_id(admin: uuid.UUID) -> uuid.UUID:
    from app.models.instance import Instance

    async with tenant_session(admin) as s:
        return (await s.execute(select(Instance.id).where(Instance.admin_id == admin))).scalar_one()


@pytest.fixture
def twilio(monkeypatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    class FakeSender:
        async def send_sms(self, from_number, to, body):
            sent.append({"from_number": from_number, "to": to, "body": body})
            return f"SM-{len(sent)}"

    from app.channels import tenant_twilio

    async def _tenant_sender(*a, **kw):
        return FakeSender()

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    return sent


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("bad", ["604-555-1234", "6045551234", "+1 604 555 1234x", "+0123456789"])
async def test_a_contact_number_that_is_not_e164_is_refused_at_save_time(client, bad):
    await _ready_account_with_luciel(client)
    r = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={"primarySms": bad, "preferredChannel": "sms"},
    )
    assert r.status_code == 422, r.text
    assert "international format" in r.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_contact_number_in_e164_saves_and_reads_back(client):
    await _ready_account_with_luciel(client)
    r = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={"primarySms": " +1 604 555 1234 ", "preferredChannel": "sms"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["escalation"]["primarySms"] == CONTACT


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_test_text_needs_a_saved_number(client, twilio):
    await _ready_account_with_luciel(client)
    r = await client.post("/api/v1/admin/luciel/escalation/test-sms", json={"which": "primary"})
    assert r.status_code == 422, r.text
    assert twilio == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_test_text_goes_out_on_the_tenants_own_number_and_is_audited(client, twilio):
    admin = await _ready_account_with_luciel(client)
    iid = await _instance_id(admin)
    async with tenant_session(admin) as s:
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=BYO_NUMBER,
            probe=NullOperabilityProbe(operable_numbers={BYO_NUMBER}),
        )
    r = await client.put(
        "/api/v1/admin/luciel/escalation", json={"primarySms": CONTACT, "preferredChannel": "sms"}
    )
    assert r.status_code == 200, r.text

    r = await client.post("/api/v1/admin/luciel/escalation/test-sms", json={"which": "primary"})
    assert r.status_code == 200, r.text
    assert r.json() == {"delivered": True, "detail": None}
    assert len(twilio) == 1
    assert twilio[0]["from_number"] == BYO_NUMBER
    assert twilio[0]["to"] == CONTACT
    assert "test" in twilio[0]["body"].lower()

    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "escalation_contact_verification_sent",
                        AdminAuditLog.event_payload["channel"].astext == "sms",
                    )
                )
            )
            .scalars()
            .all()
        )
    # Two sends were audited: saving the number started its confirmation-code loop
    # (round 7 WP-4b; through the notification leg, which this fixture does not
    # intercept), then the owner's test text went out. The test text names its slot.
    assert len(rows) == 2
    (test_row,) = [r for r in rows if r.event_payload.get("slot") == "primary"]
    assert test_row.event_payload["delivered"] is True
    assert test_row.event_payload["number_suffix"] == CONTACT[-4:]
    assert CONTACT not in str(test_row.event_payload)  # suffix only (§3.4.11)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_test_text_reports_honestly_when_the_number_cannot_send(client, twilio):
    """No attested number → the text does not go out, and the owner is told so rather
    than shown a green result."""
    await _ready_account_with_luciel(client)
    r = await client.put(
        "/api/v1/admin/luciel/escalation", json={"primarySms": CONTACT, "preferredChannel": "sms"}
    )
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/admin/luciel/escalation/test-sms", json={"which": "primary"})
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is False
    assert r.json()["detail"] == "channel_not_provisioned"
    assert twilio == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_test_texts_are_rate_limited_per_tenant(client, twilio):
    await _ready_account_with_luciel(client)
    r = await client.put(
        "/api/v1/admin/luciel/escalation", json={"primarySms": CONTACT, "preferredChannel": "sms"}
    )
    assert r.status_code == 200, r.text
    statuses = []
    for _ in range(4):
        r = await client.post("/api/v1/admin/luciel/escalation/test-sms", json={"which": "primary"})
        statuses.append(r.status_code)
    assert statuses == [200, 200, 200, 429]
