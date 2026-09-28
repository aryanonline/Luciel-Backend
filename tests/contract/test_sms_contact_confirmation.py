"""An SMS escalation contact is proven before a hot lead depends on it (round 7 WP-4b).

Email contacts had a confirmation loop since round 5B; a number had a "test text"
that persisted nothing, and Twilio accepting the request was read as the contact
hearing. Now saving an SMS contact sends a 6-digit code through the tenant's OWN
number, the owner enters it on the dashboard (or the contact texts it back — pinned
in ``tests/integration/test_contact_reply_not_a_lead.py``), the contact-health surface
reports the number like an address, and a tenant with no number connected is told
so instead of shown a green result.
"""

from __future__ import annotations

import re
import uuid

import pytest
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import attach_sms_number
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.email_incident import EscalationContactConfirmation
from app.notifications import sms_notifier
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

BYO_NUMBER = "+16045550100"
CONTACT = "+16045551234"
PUT = "/api/v1/admin/luciel/escalation"
CONFIRM = "/api/v1/admin/luciel/escalation/contacts/confirm-sms"
RESEND = "/api/v1/admin/luciel/escalation/resend-confirmation"


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"esc-code-{uuid.uuid4().hex[:8]}@x.com"
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


async def _attach_number(admin: uuid.UUID) -> None:
    from app.models.instance import Instance

    async with tenant_session(admin) as s:
        iid = (await s.execute(select(Instance.id).where(Instance.admin_id == admin))).scalar_one()
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=BYO_NUMBER,
            probe=NullOperabilityProbe(operable_numbers={BYO_NUMBER}),
        )


@pytest.fixture
def twilio(monkeypatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    class FakeSender:
        async def send_sms(self, from_number, to, body):
            sent.append({"from_number": from_number, "to": to, "body": body})
            return f"SM-{len(sent)}"

    async def _tenant_sender(*a, **kw):
        return FakeSender()

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _tenant_sender)
    return sent


def _code_in(body: str) -> str:
    match = re.search(r"\b(\d{6})\b", body)
    assert match, body
    return match.group(1)


def _health(body: dict, address: str) -> dict:
    entries = [h for h in (body.get("escalationContactHealth") or []) if h["address"] == address]
    assert entries, body.get("escalationContactHealth")
    return entries[0]


async def _audit(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_saving_a_number_sends_its_code_from_the_tenants_number_and_the_owner_confirms(
    client, twilio
):
    admin = await _ready_account_with_luciel(client)
    await _attach_number(admin)

    r = await client.put(PUT, json={"primarySms": CONTACT, "preferredChannel": "sms"})
    assert r.status_code == 200, r.text
    health = _health(r.json(), CONTACT)
    assert health["channel"] == "sms" and health["role"] == "primary"
    assert health["state"] == "pending_confirmation"
    assert health["detail"] is None

    assert len(twilio) == 1
    assert twilio[0]["from_number"] == BYO_NUMBER and twilio[0]["to"] == CONTACT
    code = _code_in(twilio[0]["body"])
    sent = await _audit(admin, "escalation_contact_verification_sent")
    assert len(sent) == 1 and sent[0].event_payload["channel"] == "sms"
    assert sent[0].event_payload["delivered"] is True
    assert code not in str(sent[0].event_payload)  # the code never rides the audit trail
    assert CONTACT not in str(sent[0].event_payload)  # suffix only (§3.4.11)

    # A re-save while the code is outstanding does not text the contact again.
    r = await client.put(PUT, json={"primarySms": CONTACT, "preferredChannel": "sms"})
    assert r.status_code == 200, r.text
    assert len(twilio) == 1

    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": wrong})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "wrong_code"
    assert _health(r.json()["luciel"], CONTACT)["state"] == "pending_confirmation"
    rejected = await _audit(admin, "escalation_contact_code_rejected")
    assert len(rejected) == 1 and rejected[0].event_payload["outcome"] == "wrong_code"
    assert wrong not in str(rejected[0].event_payload)

    r = await client.post(CONFIRM, json={"number": " +1 (604) 555-1234 ", "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "confirmed"
    confirmed = _health(r.json()["luciel"], CONTACT)
    assert confirmed["state"] == "verified" and confirmed["verifiedAt"]
    verified = await _audit(admin, "escalation_contact_verified")
    assert len(verified) == 1 and verified[0].event_payload["channel"] == "sms"
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EscalationContactConfirmation).where(
                    EscalationContactConfirmation.admin_id == admin,
                    EscalationContactConfirmation.kind == "sms",
                )
            )
        ).scalar_one()
    assert row.value_normalized == CONTACT
    assert row.confirmed_at is not None and row.code_hash is None

    # Consumed: the same code answers nothing a second time, and GET reads verified.
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": code})
    assert r.json()["outcome"] == "already_confirmed"
    assert _health((await client.get("/api/v1/admin/luciel")).json(), CONTACT)["state"] == (
        "verified"
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_five_wrong_entries_kill_the_code_and_a_resend_mints_a_new_one(client, twilio):
    from datetime import UTC, datetime, timedelta

    admin = await _ready_account_with_luciel(client)
    await _attach_number(admin)
    r = await client.put(PUT, json={"primarySms": CONTACT, "preferredChannel": "sms"})
    assert r.status_code == 200, r.text
    code = _code_in(twilio[0]["body"])
    wrong = f"{(int(code) + 7) % 1_000_000:06d}"

    outcomes = []
    for _ in range(3):  # the per-tenant bucket admits three entries in a row
        r = await client.post(CONFIRM, json={"number": CONTACT, "code": wrong})
        assert r.status_code == 200, r.text
        outcomes.append(r.json()["outcome"])
    assert outcomes == ["wrong_code"] * 3
    # Entry number four in the same window is throttled, not counted.
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": wrong})
    assert r.status_code == 429, r.text

    # Two more wrong entries (the store is per-process; a fresh one models the window
    # passing) lock the code: the right code is refused until a new one is sent.
    from app.channels import rate_limit

    rate_limit._store = rate_limit.TokenBucketStore()
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": wrong})
    assert r.json()["outcome"] == "wrong_code"
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": wrong})
    assert r.json()["outcome"] == "locked"
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": code})
    assert r.json()["outcome"] == "locked"

    # Inside the cooldown a resend is refused honestly; past it, a fresh code goes out
    # and the old one is dead.
    r = await client.post(RESEND, json={"address": CONTACT})
    assert r.status_code == 422 and "moments ago" in r.text
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EscalationContactConfirmation).where(
                    EscalationContactConfirmation.admin_id == admin
                )
            )
        ).scalar_one()
        row.last_code_sent_at = datetime.now(UTC) - timedelta(minutes=6)
    r = await client.post(RESEND, json={"address": CONTACT})
    assert r.status_code == 200, r.text
    assert len(twilio) == 2
    fresh = _code_in(twilio[1]["body"])
    assert _health(r.json(), CONTACT)["state"] == "pending_confirmation"
    # Six minutes passed for the resend; the entry bucket refilled with them.
    rate_limit._store = rate_limit.TokenBucketStore()
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": code})
    assert r.json()["outcome"] == "wrong_code"
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": fresh})
    assert r.json()["outcome"] == "confirmed"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_connected_number_is_an_honest_refusal_not_a_green_result(client, twilio):
    """The code goes out through the tenant's own number; with none connected nothing
    is sent, the contact-health surface names the reason, and a resend says what to
    connect first."""
    admin = await _ready_account_with_luciel(client)

    r = await client.put(PUT, json={"primarySms": CONTACT, "preferredChannel": "sms"})
    assert r.status_code == 200, r.text
    health = _health(r.json(), CONTACT)
    assert health["state"] == "unverified"
    assert health["detail"] == "sms_channel_not_provisioned"
    assert twilio == []
    sent = await _audit(admin, "escalation_contact_verification_sent")
    assert len(sent) == 1
    assert sent[0].event_payload["delivered"] is False
    assert sent[0].event_payload["detail"] == "sms_channel_not_provisioned"

    r = await client.post(RESEND, json={"address": CONTACT})
    assert r.status_code == 422, r.text
    assert "Connect an SMS number" in r.text
    assert twilio == []

    # Nothing outstanding to enter, and the refusal is audited rather than a 500.
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": "123456"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "no_pending"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_code_for_a_number_that_is_not_a_contact_is_refused(client, twilio):
    await _ready_account_with_luciel(client)
    r = await client.post(CONFIRM, json={"number": "+16045559999", "code": "123456"})
    assert r.status_code == 422, r.text
    assert "not currently configured" in r.text
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": "12ab56"})
    assert r.status_code == 422, r.text
