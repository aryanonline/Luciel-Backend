"""Wire fields the dashboard (round7/fe3) reads, now served (round 7 M10, item 9).

* ``KnowledgeSourceOut.pausedSince`` (the D10 clock) and ``deletedBy`` (the tombstone's
  author) — and a source the sync found gone AT ITS ORIGIN is listed, marked, until the
  retention pass purges it, so the owner reads "removed at its source — not
  restorable" on the list instead of through a failed restore.
* ``ConversationSummaryOut.countedForBillingReason`` — WHY an unbilled conversation was
  not billed, from the persisted escalation row (``budget_exhausted`` → at_capacity,
  ``llm_unavailable`` → outage).
* A carrier delivery receipt for a person's take-over reply sent as a text now lands
  on the transcript row (it updated ``escalation_events`` only): the take-over send asks
  Twilio for the receipt and keeps the MessageSid on the row (migration 0076).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.channels import tenant_twilio
from app.channels.tenant_twilio import FIELD_CAPABILITY_TOKEN
from app.channels.twilio_sender import TwilioSmsSender
from app.config import get_settings
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.budget import CountedSession
from app.models.conversation import EscalationEvent, Transcript
from app.models.knowledge import KnowledgeSource
from app.notifications.sms_notifier import NOTIFICATION_STATUS_PATH
from sqlalchemy import select

from tests.contract.test_takeover_truth import (
    LEAD_EMAIL,
    LEAD_PHONE,
    TENANT_NUMBER,
    _messages,
    _seed,
    _signup_verify_login,
    _take_over_and_reply,
)

pytestmark = [pytest.mark.contract]

RECEIPTS = "/api/v1/webhooks/twilio/notifications/status"


# --- knowledge: pausedSince + deletedBy ------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_source_list_serves_the_pause_clock_and_the_origin_tombstone(client):
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="widget", contact=LEAD_EMAIL)
    paused_at = datetime.now(UTC) - timedelta(days=2)
    live, paused, gone, owner_deleted = (uuid.uuid4() for _ in range(4))
    async with tenant_session(admin) as s:
        for sid, name, extra in (
            (live, "Hours", {}),
            (
                paused,
                "Drive playbook",
                {"paused_since": paused_at, "sync_status": "paused_reconnect_needed"},
            ),
            (
                gone,
                "Old page",
                {"soft_deleted_at": datetime.now(UTC), "deleted_by": "origin"},
            ),
            (
                owner_deleted,
                "Mis-upload",
                {"soft_deleted_at": datetime.now(UTC), "deleted_by": "owner"},
            ),
        ):
            s.add(
                KnowledgeSource(
                    id=sid,
                    admin_id=admin,
                    instance_id=seeded.instance_id,
                    name=name,
                    origin="upload",
                    source_origin="upload",
                    ingestion_status="ready",
                    **extra,
                )
            )
        await s.flush()

    r = await client.get("/api/v1/admin/knowledge/sources")
    assert r.status_code == 200, r.text
    by_id = {row["sourceId"]: row for row in r.json()}
    assert by_id[str(live)]["pausedSince"] is None
    assert by_id[str(live)]["deletedBy"] is None
    served = datetime.fromisoformat(by_id[str(paused)]["pausedSince"])
    assert abs((served - paused_at).total_seconds()) < 1
    # Gone at its origin: listed, marked, so the page says "not restorable" up front.
    assert by_id[str(gone)]["deletedBy"] == "origin"
    # The owner's own delete stays off the list, as before.
    assert str(owner_deleted) not in by_id


# --- conversations: countedForBillingReason --------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unbilled_conversation_says_why_from_what_was_persisted(client):
    admin = await _signup_verify_login(client)
    now = datetime.now(UTC)
    billed = await _seed(admin, channel="widget", contact=LEAD_EMAIL, last_message_at=now)
    at_cap = await _seed(
        admin, channel="widget", contact=LEAD_EMAIL, last_message_at=now - timedelta(minutes=1)
    )
    outage = await _seed(
        admin, channel="widget", contact=LEAD_EMAIL, last_message_at=now - timedelta(minutes=2)
    )
    silent = await _seed(
        admin, channel="widget", contact=LEAD_EMAIL, last_message_at=now - timedelta(minutes=3)
    )
    async with tenant_session(admin) as s:
        s.add(
            CountedSession(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=billed.instance_id,
                session_id=billed.session_id,
                billing_period_start=now.date(),
            )
        )
        # What the turns persisted: the at-cap reply's row, and an outage's row.
        for seeded, signal in ((at_cap, "budget_exhausted"), (outage, "llm_unavailable")):
            s.add(
                EscalationEvent(
                    admin_id=admin,
                    instance_id=seeded.instance_id,
                    session_id=seeded.session_id,
                    signal=signal,
                    gate="outcome",
                    fired_at=now,
                )
            )
        # A billed session that ALSO saw an outage later still has nothing to explain.
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=billed.instance_id,
                session_id=billed.session_id,
                signal="llm_unavailable",
                gate="outcome",
                fired_at=now,
            )
        )
        await s.flush()

    rows = (await client.get("/api/v1/dashboard/conversations")).json()
    reasons = {r["sessionId"]: (r["countedForBilling"], r["countedForBillingReason"]) for r in rows}
    assert reasons[str(billed.session_id)] == (True, None)
    assert reasons[str(at_cap.session_id)] == (False, "at_capacity")
    assert reasons[str(outage.session_id)] == (False, "outage")
    assert reasons[str(silent.session_id)] == (False, None)


# --- the carrier's receipt on a person's text reply ------------------------------------


class _FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        sid = f"SM{len(self.calls):032d}"

        class _Msg:
            pass

        msg = _Msg()
        msg.sid = sid  # type: ignore[attr-defined]
        return msg


class _FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


@pytest.fixture
def live_twilio(monkeypatch) -> _FakeTwilioClient:
    """The tenant's REAL sender class over a fake HTTP client, so the request the
    take-over send makes (status callback included) is what Twilio would receive."""
    client = _FakeTwilioClient()
    sender = TwilioSmsSender(account_sid="AC" + "7" * 32, auth_token="tok")  # noqa: S106
    sender._client = client

    async def _tenant_sender(*a, **kw):
        return sender

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    monkeypatch.setattr(get_settings(), "public_api_base_url", "https://api.example")
    return client


async def _reply_row(admin: uuid.UUID, message_id: str) -> Transcript:
    async with tenant_session(admin) as s:
        return (
            await s.execute(select(Transcript).where(Transcript.id == uuid.UUID(message_id)))
        ).scalar_one()


def _receipt(sid: str, status: str, error_code: str | None = None) -> dict[str, str]:
    form = {"From": TENANT_NUMBER, "To": LEAD_PHONE, "MessageSid": sid, "MessageStatus": status}
    if error_code:
        form["ErrorCode"] = error_code
    return form


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_carrier_receipt_for_a_human_text_reply_lands_on_its_transcript_row(
    client, live_twilio
):
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="sms",
        contact=LEAD_PHONE,
        sms_status="connected",
        transport_sender=LEAD_PHONE,
    )
    # A capability-verified tenant (C11): the receipt route can verify its receipts.
    await get_secret_store().put(
        secret_ref_for(admin, seeded.instance_id, "sms_sender"),
        {"access_token": "bearer-tok", FIELD_CAPABILITY_TOKEN: "status-cap"},
    )

    body = await _take_over_and_reply(client, seeded.session_id)
    assert body["delivered"] is True
    # The send asked the carrier for its receipt, on the tenant's verified route...
    (sent,) = live_twilio.messages.calls
    assert (
        sent["status_callback"] == f"https://api.example{NOTIFICATION_STATUS_PATH}?cap=status-cap"
    )
    # ...and the row keeps the SID the receipt will name.
    row = await _reply_row(admin, body["message"]["messageId"])
    sid = str(row.delivery_provider_message_id)
    assert sid.startswith("SM") and row.delivery_status == "delivered"

    # An interim status changes nothing.
    r = await client.post(f"{RECEIPTS}?cap=status-cap", data=_receipt(sid, "sent"))
    assert r.status_code == 200
    assert (await _reply_row(admin, body["message"]["messageId"])).delivery_status == "delivered"

    # The carrier could not deliver it: the dashboard stops reading it as sent.
    r = await client.post(
        f"{RECEIPTS}?cap=status-cap", data=_receipt(sid, "undelivered", error_code="30003")
    )
    assert r.status_code == 200
    (msg,) = await _messages(client, seeded.session_id)
    assert msg["deliveryStatus"] == "failed"
    assert msg["deliveryDetail"] == "send_failed"
    assert msg["deliveredAt"] is None
    failed = await _reply_row(admin, body["message"]["messageId"])
    assert failed.delivery_detail == "sms_dlr:undelivered:30003"
    # A retried receipt does not resurrect a settled failure.
    await client.post(f"{RECEIPTS}?cap=status-cap", data=_receipt(sid, "delivered"))
    assert (await _reply_row(admin, body["message"]["messageId"])).delivery_status == "failed"

    # A second reply that the carrier DID deliver is stamped with the carrier's time.
    r = await client.post(
        f"/api/v1/dashboard/conversations/{seeded.session_id}/messages",
        json={"text": "One more thing — bring the plans."},
    )
    assert r.status_code == 200, r.text
    second = await _reply_row(admin, r.json()["message"]["messageId"])
    accepted_at = second.delivered_at
    r = await client.post(
        f"{RECEIPTS}?cap=status-cap",
        data=_receipt(str(second.delivery_provider_message_id), "delivered"),
    )
    assert r.status_code == 200
    confirmed = await _reply_row(admin, str(second.id))
    assert confirmed.delivery_status == "delivered"
    assert confirmed.delivered_at is not None and accepted_at is not None
    assert confirmed.delivered_at >= accepted_at
