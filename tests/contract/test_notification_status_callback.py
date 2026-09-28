"""Twilio's delivery receipt for an owner-notification text stamps the escalation row
(round 7 WP-4b): ``delivered`` means the carrier delivered it, ``failed`` /
``undelivered`` pages the owner by email now and the row says ``owner_fallback`` (or
``failed`` when the owner cannot be reached either), an interim status changes nothing,
and a receipt for a message this platform never sent is a 200 no-op. Same posture as the
voice status route: signature or capability-URL verified, per-number flood control, a
forged ``cap`` 403s and is audited under the tenant it names.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.channels.tenant_twilio import FIELD_CAPABILITY_TOKEN
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent
from app.models.instance import Account, AccountState, Instance, LucielState
from app.notifications import email_notifier
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

ROUTE = "/api/v1/webhooks/twilio/notifications/status"
OWNER_EMAIL = "owner@acme.example"
CONTACT = "+16045551234"


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        self.sent.append({"to": to, "subject": subject, "body": body_text})
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch) -> FakeEmailSender:
    fake = FakeEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _seed(
    number: str, *, sid: str, detail: str = "sms:primary"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A capability-verified tenant (C11) with one page out on ``sid``, row ``sent``."""
    admin, iid, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ref = secret_ref_for(admin, iid, "sms_sender")
    await get_secret_store().put(
        ref, {"access_token": "bearer-tok", FIELD_CAPABILITY_TOKEN: "status-cap"}
    )
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=OWNER_EMAIL,
                email_lower=OWNER_EMAIL,
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Acme Patios",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={"primarySms": CONTACT, "preferredChannel": "sms"},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                provider="twilio",
                auth_class="oauth_token",
                status="connected",
                secret_ref=ref,
                non_secret_config={"accountSid": "AC" + "5" * 32, "destination": number},
            )
        )
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                session_id=session_id,
                signal="high_value_lead",
                gate="outcome",
                score_or_confidence=0.9,
                reasons=["named a budget"],
                fired_at=datetime.now(UTC),
                delivery_status="sent",
                delivery_detail=detail,
                delivery_provider_message_id=sid,
            )
        )
        await s.flush()
    return admin, session_id


async def _row(admin: uuid.UUID, session_id: uuid.UUID) -> EscalationEvent:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(EscalationEvent).where(
                    EscalationEvent.admin_id == admin, EscalationEvent.session_id == session_id
                )
            )
        ).scalar_one()


async def _audit(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type
                    )
                )
            )
            .scalars()
            .all()
        )


def _receipt(number: str, *, sid: str, status: str, error_code: str | None = None) -> dict:
    form = {"From": number, "To": CONTACT, "MessageSid": sid, "MessageStatus": status}
    if error_code:
        form["ErrorCode"] = error_code
    return form


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_carriers_delivered_stamps_the_row_delivered(client, ses):
    number, sid = "+16045550181", "SM" + "a" * 32
    admin, session_id = await _seed(number, sid=sid)

    interim = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="sent")
    )
    assert interim.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "sent" and row.delivered_at is None  # nothing yet

    ok = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="delivered")
    )
    assert ok.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "delivered"
    assert row.delivered_at is not None
    assert ses.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_receipt_pages_the_owner_by_email_and_says_why(client, ses):
    number, sid = "+16045550182", "SM" + "b" * 32
    admin, session_id = await _seed(number, sid=sid)

    r = await client.post(
        f"{ROUTE}?cap=status-cap",
        data=_receipt(number, sid=sid, status="undelivered", error_code="30003"),
    )
    assert r.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "owner_fallback"
    assert row.delivery_detail == "sms_dlr:undelivered:30003"
    assert [m["to"] for m in ses.sent] == [OWNER_EMAIL]
    assert "named a budget" in str(ses.sent[0]["body"])
    failed = await _audit(admin, "escalation_delivery_failed")
    assert len(failed) == 1
    assert failed[0].event_payload["failure_reason"] == "sms_dlr:undelivered:30003"
    assert failed[0].event_payload["provider_message_id"] == sid
    fallback = await _audit(admin, "escalation_owner_fallback")
    assert len(fallback) == 1
    assert fallback[0].event_payload["reason"] == "sms_delivery_failed"

    # Twilio retries receipts: the second one for a settled row pages nobody again.
    again = await client.post(
        f"{ROUTE}?cap=status-cap",
        data=_receipt(number, sid=sid, status="undelivered", error_code="30003"),
    )
    assert again.status_code == 200
    assert len(ses.sent) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_receipt_for_a_page_the_owner_was_already_copied_on_does_not_page_twice(
    client, ses
):
    """An unconfirmed number had the owner copied when the page went out; the carrier
    later failing it must not email the owner a second time — the row just says so."""
    number, sid = "+16045550183", "SM" + "c" * 32
    admin, session_id = await _seed(number, sid=sid, detail="sms:primary+owner")

    r = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid=sid, status="failed")
    )
    assert r.status_code == 200
    row = await _row(admin, session_id)
    assert row.delivery_status == "owner_fallback"
    assert row.delivery_detail == "sms_dlr:failed"
    assert ses.sent == []
    assert len(await _audit(admin, "escalation_delivery_failed")) == 1
    assert await _audit(admin, "escalation_owner_fallback") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_receipt_for_a_message_this_platform_never_sent_is_a_no_op(client, ses):
    number, sid = "+16045550184", "SM" + "d" * 32
    admin, session_id = await _seed(number, sid=sid)

    r = await client.post(
        f"{ROUTE}?cap=status-cap", data=_receipt(number, sid="SM" + "0" * 32, status="delivered")
    )
    assert r.status_code == 200
    assert (await _row(admin, session_id)).delivery_status == "sent"
    assert ses.sent == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_forged_cap_403s_and_is_audited_under_the_tenant(client, ses):
    number, sid = "+16045550185", "SM" + "e" * 32
    admin, session_id = await _seed(number, sid=sid)

    forged = await client.post(
        f"{ROUTE}?cap=wrong", data=_receipt(number, sid=sid, status="delivered")
    )
    assert forged.status_code == 403
    assert len(await _audit(admin, "inbound_capability_rejected")) == 1
    assert (await _row(admin, session_id)).delivery_status == "sent"
