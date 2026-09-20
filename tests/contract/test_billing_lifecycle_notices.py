"""Billing lifecycle notices say what actually happened (Architecture §3.6.7 / Legal §A3;
round 7 WP-1 #3).

  (a) the scheduled day-7 reduction (``dunning.advance_if_due``) sends the FINAL
      reduction notice — it used to drop the account to the free allowance silently;
  (b) the dunning body branches on the state: ``retrying`` states the 1/3/5/7 schedule
      and what runs out after it, ``reduced`` states what has already happened;
  (c) a card removal — the admin's DELETE and Stripe's payment_method.detached — sends a
      notice by email and, when an escalation SMS number is on file, by text through
      the owner's own Twilio (the escalation leg), each audited as
      ``lifecycle_notice_sent`` with its own delivery status.

Mock posture throughout: the email sender is the Null sender (status ``recorded``), the
tenant SMS sender is a captured NullSmsSender. No network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.billing import dunning
from app.billing import webhooks as billing_webhooks
from app.billing.metering import get_or_create_billing_account
from app.billing.stripe_gateway import FakeStripeGateway, StripeEvent, set_stripe_gateway
from app.channels.twilio_sender import NullSmsSender
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState
from app.notifications import email_dispatcher, sms_notifier
from app.notifications.email_sender import NullEmailSender
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

SMS_NUMBER = "+16045551234"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"notice-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_instance(admin: uuid.UUID, *, sms_number: str | None = None) -> uuid.UUID:
    """One Instance; with ``sms_number`` the escalation contact AND a provisioned
    sms_sender connection (the owner's own number) are on file."""
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Acme Patios",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={"primarySms": sms_number} if sms_number else {},
                personality={},
            )
        )
        if sms_number:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="sms_sender",
                    auth_class="api_key",
                    provider="twilio",
                    status="connected",
                    secret_ref=secret_ref_for(admin, iid, "sms_sender"),
                    non_secret_config={"destination": "+16045550000"},
                )
            )
    return iid


async def _notices(admin: uuid.UUID) -> list[dict[str, object]]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "lifecycle_notice_sent",
                    )
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload) for r in rows]


def _billing_mails(sender: NullEmailSender) -> list:
    """The billing notices only — the signup flow's verification mail rides the same
    sender and is filtered out by its SES purpose tag."""
    return [m for m in sender.sent if (m.tags or {}).get("vm_purpose") != "verify_email"]


@pytest.fixture
def mail(monkeypatch) -> NullEmailSender:
    """Capture every platform email the dispatcher sends (mock posture → Null sender)."""
    sender = NullEmailSender()
    monkeypatch.setattr(email_dispatcher, "build_email_sender", lambda: sender)
    return sender


@pytest.fixture
def sms(monkeypatch) -> NullSmsSender:
    """Capture the tenant-Twilio leg: the notifier builds the sender per send, so hand it
    one instance to inspect."""
    sender = NullSmsSender()

    async def _build(session, admin_id, instance_id, *, store=None):
        return sender

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _build)
    return sender


# --- (a) the scheduled reduction is announced --------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_scheduled_day7_reduction_sends_the_final_notice(client, mail) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.dunning_state = dunning.RETRYING
        acct.dunning_started_at = NOW - timedelta(days=8)
        await s.flush()

    async with tenant_session(admin) as s:
        assert await dunning.advance_if_due(s, admin_id=admin, now=NOW) == dunning.REDUCED

    notices = await _notices(admin)
    assert [n["notice_type"] for n in notices] == ["dunning_reduced"]
    (notice,) = notices
    assert notice["delivery_method"] == "email"
    assert notice["delivery_status"] == "recorded"  # Null sender in mock posture
    assert notice["dunning_state"] == "reduced"
    assert notice["attempt_number"] is None  # no Stripe retry behind the schedule
    (sent,) = _billing_mails(mail)
    assert "free allowance" in sent.body
    assert "paused until a valid card is added" in sent.body
    assert "already in progress is never cut off" in sent.body

    # Idempotent with the transition: a second sweep pass sends nothing more.
    async with tenant_session(admin) as s:
        assert await dunning.advance_if_due(s, admin_id=admin, now=NOW) is None
    assert len(await _notices(admin)) == 1


# --- (b) the body states the consequence for the state ------------------------ #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retry_notice_states_the_schedule_and_what_runs_out(client, mail) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    event = StripeEvent(
        event_id="evt_retry_2", type="invoice.payment_failed", data={"attempt_count": 2}
    )
    async with tenant_session(admin) as s:
        assert (
            await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=NOW)
            == "payment_failed"
        )

    (notice,) = await _notices(admin)
    assert notice["notice_type"] == "payment_failed_retry"
    assert notice["dunning_state"] == "retrying"
    assert notice["attempt_number"] == 2
    (sent,) = _billing_mails(mail)
    assert "retry attempt 2" in sent.body
    assert "days 1, 3, 5 and 7" in sent.body
    assert "only the free 50 conversations" in sent.body
    assert "action tools" in sent.body and "pause" in sent.body
    # D1: the owner's own channels are theirs; the notice never threatens them.
    assert "Your own email and SMS channels keep working" in sent.body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_day7_failure_webhook_sends_the_reduction_notice_not_a_retry(client, mail) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.dunning_state = dunning.RETRYING
        acct.dunning_started_at = NOW - timedelta(days=8)
        await s.flush()
    event = StripeEvent(
        event_id="evt_retry_4", type="invoice.payment_failed", data={"attempt_count": 4}
    )
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=NOW)

    (notice,) = await _notices(admin)
    assert notice["notice_type"] == "dunning_reduced"
    assert notice["dunning_state"] == "reduced"
    (sent,) = _billing_mails(mail)
    assert sent.subject == "Your VantageMind account is now on the free allowance"
    assert "now on the free allowance" in sent.body
    assert "retry attempt" not in sent.body


# --- (c) a card removal is announced on both paths ---------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_admin_card_removal_notifies_by_email_and_escalation_sms(client, mail, sms) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, sms_number=SMS_NUMBER)
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        await s.flush()

    set_stripe_gateway(FakeStripeGateway())
    try:
        r = await client.delete("/api/v1/billing/payment-method")
        assert r.status_code == 200
    finally:
        set_stripe_gateway(None)

    notices = await _notices(admin)
    assert [(n["notice_type"], n["delivery_method"]) for n in notices] == [
        ("payment_method_removed", "email"),
        ("payment_method_removed", "sms"),
    ]
    email_notice, sms_notice = notices
    assert email_notice["reason"] == "admin_removed"
    assert email_notice["delivery_status"] == "recorded"
    assert sms_notice["recipient"] == SMS_NUMBER
    assert sms_notice["delivery_status"] == "recorded"  # Null sender outside live mode
    assert sms_notice["delivery_degraded"] is False

    (sent,) = _billing_mails(mail)
    assert "was removed from your account" in sent.body
    assert "new conversations wait until a card is added" in sent.body
    assert "Nothing else changes" in sent.body
    # The text went out through the owner's OWN number, to the escalation contact.
    (text,) = sms.sent
    assert text.to == SMS_NUMBER
    assert text.from_number == "+16045550000"
    assert text.body.startswith("VantageMind:")
    assert "free 50" in text.body and "/dashboard/billing" in text.body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_card_removal_without_an_sms_contact_is_email_only(client, mail, sms) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    set_stripe_gateway(FakeStripeGateway())
    try:
        assert (await client.delete("/api/v1/billing/payment-method")).status_code == 200
    finally:
        set_stripe_gateway(None)

    notices = await _notices(admin)
    assert [n["delivery_method"] for n in notices] == ["email"]
    assert sms.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_stripe_detach_notifies_and_names_the_stripe_side_removal(client, mail, sms) -> None:
    """The detach the owner may NOT have made (an issuer pulling the card) is the one
    that must not be silent."""
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, sms_number=SMS_NUMBER)
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        await s.flush()
    event = StripeEvent(event_id="evt_detach_n", type="payment_method.detached", data={})
    async with tenant_session(admin) as s:
        assert (
            await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=NOW)
            == "payment_method.detached"
        )

    notices = await _notices(admin)
    assert [(n["notice_type"], n["delivery_method"]) for n in notices] == [
        ("payment_method_removed", "email"),
        ("payment_method_removed", "sms"),
    ]
    assert all(n["reason"] == "stripe_detached" for n in notices)
    (sent,) = _billing_mails(mail)
    assert "was removed from your Stripe customer" in sent.body
    assert len(sms.sent) == 1

    # Replay-gated: the same event id again sends nothing more (HARD CONSTRAINT §3.4.1b).
    async with tenant_session(admin) as s:
        assert (
            await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=NOW)
            == "duplicate"
        )
    assert len(await _notices(admin)) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_undeliverable_sms_leg_is_a_failed_record_never_a_fabricated_send(
    client, mail, sms
) -> None:
    """The escalation number is on file but the owner's Twilio number is not provisioned:
    the text cannot go out and the audit says so — the email leg still lands."""
    admin = await _signup_verify_login(client)
    iid = await _seed_instance(admin, sms_number=SMS_NUMBER)
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin, InstanceConnection.instance_id == iid
                )
            )
        ).scalar_one()
        conn.non_secret_config = {}
        await s.flush()
    set_stripe_gateway(FakeStripeGateway())
    try:
        assert (await client.delete("/api/v1/billing/payment-method")).status_code == 200
    finally:
        set_stripe_gateway(None)

    email_notice, sms_notice = await _notices(admin)
    assert email_notice["delivery_method"] == "email"
    assert sms_notice["delivery_method"] == "sms"
    assert sms_notice["delivery_status"] == "failed"
    assert sms_notice["delivery_degraded"] is True
    assert sms_notice["failure_reason"] == "sms_channel_not_provisioned"
    assert sms_notice["message_id"] is None
    assert sms.sent == []
