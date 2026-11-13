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

pytestmark = [pytest.mark.contract]

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

    # All five entries are answered by the code loop itself, back to back — the
    # throttle never speaks first (round 7 delivery hardening: it used to 429 the
    # fourth). The fifth wrong entry locks the code: the right code is refused until
    # a new one is sent.
    outcomes = []
    for _ in range(5):
        r = await client.post(CONFIRM, json={"number": CONTACT, "code": wrong})
        assert r.status_code == 200, r.text
        outcomes.append(r.json()["outcome"])
    assert outcomes == ["wrong_code"] * 4 + ["locked"]
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": code})
    assert r.status_code == 200, r.text
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


TEAM = "/api/v1/admin/luciel/team-availability"
AFTER_HOURS = "+16045557777"


def _after_hours(channel: str, contact: str) -> dict:
    key = "contactSms" if channel == "sms" else "contactEmail"
    return {
        "timezone": "UTC",
        "enabled": True,
        "afterHours": {"escalation": {"channel": channel, key: contact}},
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_after_hours_number_is_proven_like_any_other_contact(client, twilio):
    """Item 2: saving the after-hours contact starts its loop, contact health lists
    it under its own role, resend covers it, and the dashboard entry confirms it."""
    from datetime import UTC, datetime, timedelta

    admin = await _ready_account_with_luciel(client)
    await _attach_number(admin)

    r = await client.put(TEAM, json=_after_hours("sms", AFTER_HOURS))
    assert r.status_code == 200, r.text
    assert len(twilio) == 1
    assert twilio[0]["from_number"] == BYO_NUMBER and twilio[0]["to"] == AFTER_HOURS
    assert _code_in(twilio[0]["body"])
    health = _health(r.json(), AFTER_HOURS)
    assert health["role"] == "after_hours" and health["channel"] == "sms"
    assert health["state"] == "pending_confirmation"

    # A re-save with the same contact does not text it again.
    r = await client.put(TEAM, json=_after_hours("sms", AFTER_HOURS))
    assert r.status_code == 200 and len(twilio) == 1

    # Resend knows the after-hours number (past the cooldown a fresh code goes out).
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EscalationContactConfirmation).where(
                    EscalationContactConfirmation.admin_id == admin
                )
            )
        ).scalar_one()
        row.last_code_sent_at = datetime.now(UTC) - timedelta(minutes=6)
    r = await client.post(RESEND, json={"address": AFTER_HOURS})
    assert r.status_code == 200, r.text
    assert len(twilio) == 2 and twilio[1]["to"] == AFTER_HOURS
    fresh = _code_in(twilio[1]["body"])

    r = await client.post(CONFIRM, json={"number": AFTER_HOURS, "code": fresh})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "confirmed"
    assert _health(r.json()["luciel"], AFTER_HOURS)["state"] == "verified"
    assert _health((await client.get("/api/v1/admin/luciel")).json(), AFTER_HOURS)["role"] == (
        "after_hours"
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_after_hours_address_gets_the_link_loop_and_shows_in_contact_health(
    client, twilio
):
    from app.models.email_incident import EmailDeliveryIncident

    admin = await _ready_account_with_luciel(client)
    address = "night-desk@shop.com"
    r = await client.put(TEAM, json=_after_hours("email", address))
    assert r.status_code == 200, r.text
    health = _health(r.json(), address)
    assert health["role"] == "after_hours" and health["channel"] == "email"
    assert health["state"] == "pending_confirmation"
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(EmailDeliveryIncident).where(EmailDeliveryIncident.admin_id == admin)
            )
        ).scalar_one()
        assert row.email_lower == address and row.verification_pending_jti
        mirror = (
            await s.execute(
                select(EscalationContactConfirmation).where(
                    EscalationContactConfirmation.admin_id == admin,
                    EscalationContactConfirmation.kind == "email",
                )
            )
        ).scalar_one()
        assert mirror.value_normalized == address and mirror.confirmed_at is None
    assert twilio == []  # an address is never texted

    # Resend honours the after-hours address too (inside the cooldown: refused honestly).
    r = await client.post(RESEND, json={"address": address})
    assert r.status_code == 422 and "moments ago" in r.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_code_texts_spend_a_per_tenant_budget_so_a_save_loop_is_not_a_texting_tool(
    client, twilio
):
    """Round 7 delivery hardening. PUT /escalation (and team-availability) texted a code
    to every unconfirmed number in the request with no ceiling but a per-number
    cooldown — the test-text route next door is limited "so it cannot be turned into a
    texting tool", these were not: a loop of saves with two fresh numbers each sent two
    texts to arbitrary phones per call, unbounded. Code texts now spend a per-tenant
    budget; past it nothing is texted, the refusal is audited like any refused send,
    the number reads unverified, and a resend says why with a 429."""
    from app.channels import rate_limit
    from app.notifications import contact_confirmation

    admin = await _ready_account_with_luciel(client)
    await _attach_number(admin)
    budget = rate_limit.LIMITS[contact_confirmation.CODE_SEND_LIMIT].burst
    numbers = [f"+1604555{3000 + i:04d}" for i in range(budget + 4)]

    for primary, secondary in zip(numbers[::2], numbers[1::2], strict=True):
        r = await client.put(
            PUT, json={"primarySms": primary, "secondarySms": secondary, "preferredChannel": "sms"}
        )
        assert r.status_code == 200, r.text  # the save itself is never refused

    assert len(twilio) == budget
    assert [t["to"] for t in twilio] == numbers[:budget]
    sent = await _audit(admin, "escalation_contact_verification_sent")
    refused = [e for e in sent if e.event_payload["detail"] == "code_send_rate_limited"]
    assert len(refused) == len(numbers) - budget
    assert all(e.event_payload["delivered"] is False for e in refused)
    assert _health(r.json(), numbers[-1])["state"] == "unverified"

    r = await client.post(RESEND, json={"address": numbers[-1]})
    assert r.status_code == 429, r.text
    assert "Too many confirmation codes" in r.text
    assert len(twilio) == budget


async def test_the_code_text_bounds_the_tenant_controlled_name():
    """The Luciel's name rides the code text and had no length bound on the wire: a
    long name made every code a multi-segment message on the tenant's account."""
    from app.notifications.contact_confirmation import _code_body

    body = _code_body("123456", "Acme  Patios\nand " + "x" * 400)
    assert "123456" in body and len(body) < 260
    assert "Acme Patios and" in body and "\n" not in body
    assert body.isascii()  # a truncation mark must not turn the text into UCS-2
    assert _code_body("123456", None).count("a Luciel") == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_entry_throttle_answers_after_every_codes_attempts_never_before(client, twilio):
    """Round 7 delivery hardening. confirm-sms borrowed the test text's bucket: three
    entries per 30 minutes per tenant, against five attempts per code and three SMS
    contacts (primary, secondary, after-hours) to confirm. One typo and the next entry
    was a 429 for up to half an hour, by which time the 15-minute code had expired —
    the documented "five wrong entries kill the code" was unreachable. Now every
    attempt of every contact's code is answered by the code loop, and only an entry
    past all of them meets the flood throttle."""
    from app.channels import rate_limit
    from app.notifications import contact_confirmation

    limits = rate_limit.LIMITS[contact_confirmation.CODE_ENTRY_LIMIT]
    attempts = contact_confirmation.MAX_CODE_ATTEMPTS
    assert limits.burst == contact_confirmation.SMS_CONTACT_SLOTS * attempts
    # On Redis the bucket is a fixed window of burst x refill: at least a code's life.
    assert limits.burst * limits.refill_seconds >= contact_confirmation.CODE_TTL.total_seconds()

    admin = await _ready_account_with_luciel(client)
    await _attach_number(admin)
    secondary = "+16045555678"
    r = await client.put(
        PUT, json={"primarySms": CONTACT, "secondarySms": secondary, "preferredChannel": "sms"}
    )
    assert r.status_code == 200, r.text
    r = await client.put(TEAM, json=_after_hours("sms", AFTER_HOURS))
    assert r.status_code == 200, r.text
    assert [t["to"] for t in twilio] == [CONTACT, secondary, AFTER_HOURS]
    codes = {t["to"]: _code_in(t["body"]) for t in twilio}

    for number in (CONTACT, secondary, AFTER_HOURS):
        wrong = f"{(int(codes[number]) + 3) % 1_000_000:06d}"
        outcomes = []
        for _ in range(attempts):
            r = await client.post(CONFIRM, json={"number": number, "code": wrong})
            assert r.status_code == 200, (number, r.text)
            outcomes.append(r.json()["outcome"])
        assert outcomes == ["wrong_code"] * (attempts - 1) + ["locked"], number

    rejected = await _audit(admin, "escalation_contact_code_rejected")
    assert len(rejected) == contact_confirmation.SMS_CONTACT_SLOTS * attempts

    # Past every attempt of every code, a flood is still throttled — and not counted.
    r = await client.post(CONFIRM, json={"number": CONTACT, "code": codes[CONTACT]})
    assert r.status_code == 429, r.text
    assert "Too many code entries" in r.text
    assert len(await _audit(admin, "escalation_contact_code_rejected")) == len(rejected)
