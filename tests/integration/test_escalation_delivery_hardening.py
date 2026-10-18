"""Escalation delivery hardening (round 7 Phase 2 review, dimension ``delivery``).

Each test models the live condition a reviewer found:

* a queued retry whose owner already heard and whose contact has nothing retryable left
  used to be replayed for 24 hours doing nothing, sat in the backlog the whole time and
  ended in a "could not deliver" email to an owner who had heard within a minute — it
  now completes once, as ``settled:owner_heard``, and the row keeps saying
  ``owner_fallback``.
* an escalation whose off-turn notification task died (a deploy's shutdown cancelled
  it) stayed ``pending`` forever: not retried, not in the backlog, no alarm — now the
  deferred sweep audits the loss, retries it once through the normal path, and the
  backlog counts it meanwhile.
* a page whose delivery receipt never came read ``sent`` forever — now the sweep asks
  the tenant's own Twilio once and stamps the answer; with no answer the row is
  ``unconfirmed`` and the owner is told.
* a failed receipt could email the owner a second time (after the cc fan-out, or after
  an owner fallback a replay re-stamped) — the row remembers the owner heard, once.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.jobs import sweeps
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent
from app.models.deferred import DeferredAction
from app.models.email_incident import EmailDeliveryIncident, EscalationContactConfirmation
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from app.notifications import email_dispatcher, email_notifier, escalation_dispatch, sms_notifier
from app.notifications.backlog import degraded_entry
from app.notifications.escalation_dispatch import (
    ESCALATION_RETRY_ACTION,
    NOTIFICATION_LOST,
    PENDING_STALE_AFTER,
    SENT_UNCONFIRMED_AFTER,
    SETTLED_OWNER_HEARD,
)
from app.runtime.integration_status import escalation_backlog
from app.runtime.orchestrator import _record_escalation
from sqlalchemy import select, update

pytestmark = [pytest.mark.asyncio]

OWNER_EMAIL = "owner@acme.example"
CONTACT = "sales@acme.example"
CONTACT_SMS = "+16045551234"
BYO_NUMBER = "+16045550100"


class SelectiveEmailSender:
    """SES as it misbehaves: refuses some recipients, or everyone, until told otherwise.
    Bound for BOTH the escalation notifier and the platform notice dispatcher, so an
    abandon notice ("could not deliver ...") is seen here too."""

    def __init__(self) -> None:
        self.refuse: set[str] | None = set()  # None → refuse everyone
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        if self.refuse is None or to in self.refuse:
            raise RuntimeError("throttled")
        self.sent.append({"to": to, "subject": subject, "body": body_text})
        return f"ses-{len(self.sent)}"

    @property
    def recipients(self) -> list[str]:
        return [m["to"] for m in self.sent]


@pytest.fixture
def ses(monkeypatch) -> SelectiveEmailSender:
    fake = SelectiveEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    monkeypatch.setattr(email_dispatcher, "build_email_sender", lambda: fake)
    return fake


class FakeTenantTwilio:
    """The tenant's own Twilio as the page and the sweep see it: a real provider
    sender (so an accepted send is ``dlr_pending`` and the row reads ``sent``) that
    refuses when told to, and answers the sweep's one status fetch the way the test
    says — a terminal word, nothing terminal yet, or no answer at all."""

    def __init__(self) -> None:
        self.refuse = False
        self.sent: list[dict[str, str]] = []
        self.status: tuple[str, str | None] | None = None
        self.fetch_raises: Exception | None = None
        self.fetched: list[str] = []

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        if self.refuse:
            raise RuntimeError("twilio 503")
        self.sent.append({"from_number": from_number, "to": to, "body": body})
        return f"SM{len(self.sent):032d}"

    async def fetch_message_status(self, message_sid: str) -> tuple[str, str | None] | None:
        self.fetched.append(message_sid)
        if self.fetch_raises is not None:
            raise self.fetch_raises
        return self.status


@pytest.fixture
def twilio(monkeypatch) -> FakeTenantTwilio:
    fake = FakeTenantTwilio()

    async def _sender(*a, **kw):
        return fake

    async def _from_number(*a, **kw):
        return BYO_NUMBER

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _sender)
    monkeypatch.setattr(sms_notifier, "resolve_sms_number", _from_number)
    return fake


async def _seed(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    escalation: dict[str, object],
    verified_emails: tuple[str, ...] = (),
    confirmed_numbers: tuple[str, ...] = (),
) -> uuid.UUID:
    lead_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        for address in verified_emails:
            s.add(
                EmailDeliveryIncident(
                    admin_id=admin, email_lower=address, verified_at=datetime.now(UTC)
                )
            )
        for number in confirmed_numbers:
            s.add(
                EscalationContactConfirmation(
                    admin_id=admin,
                    kind="sms",
                    value_normalized=number,
                    confirmed_at=datetime.now(UTC),
                )
            )
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=uuid.uuid4(),
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
                escalation=escalation,
            )
        )
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Dana Lee",
                contact_identifier="dana@lead.example",
                intent="Wants a quote",
            )
        )
        await s.flush()
    return lead_id


async def _fire(admin: uuid.UUID, iid: uuid.UUID, sid: uuid.UUID, lead_id: uuid.UUID) -> None:
    """The runtime's own write + the off-turn notification it schedules, drained."""
    async with tenant_session(admin) as s:
        await _record_escalation(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            resolved_lead_id=lead_id,
            signal="high_value_lead",
            gate="outcome",
            score=0.91,
        )
    await escalation_dispatch.drain_background_notifications()


async def _row(admin: uuid.UUID, sid: uuid.UUID) -> EscalationEvent:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(EscalationEvent).where(
                    EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid
                )
            )
        ).scalar_one()


async def _items(admin: uuid.UUID) -> list[DeferredAction]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(DeferredAction).where(
                        DeferredAction.admin_id == admin,
                        DeferredAction.action_type == ESCALATION_RETRY_ACTION,
                    )
                )
            )
            .scalars()
            .all()
        )


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


async def _abandons(admin: uuid.UUID) -> list[AdminAuditLog]:
    return [
        e
        for e in await _audit(admin, "escalation_delivery_failed")
        if e.event_payload.get("action") == "deferred_abandoned"
    ]


def _backlog_data(captured: str) -> list[float]:
    return [
        json.loads(line)["EscalationBacklog"]
        for line in captured.splitlines()
        if "EscalationBacklog" in line
    ]


# --- a stand-down completes its queue item, once, honestly ---------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_owner_blip_healed_by_the_ladder_completes_the_queued_item(
    ses, monkeypatch, capsys
):
    """The default tenant after signup has no escalation contact. SES throttles the
    owner email once, so the page is queued; the ladder's rung delivers it half a
    minute later. The queued item has nothing left to do: it is completed right there —
    not replayed hourly for 24h, counted in the backlog, and ended with an email
    telling the owner (who heard) that the alert could not be delivered."""
    monkeypatch.setattr(escalation_dispatch, "ESCALATION_RETRY_DELAYS", (0.25,))
    ses.refuse = None
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation={})

    await _fire(admin, iid, sid, lead)
    assert (await _row(admin, sid)).delivery_status == "queued"
    (item,) = await _items(admin)
    assert item.status == "queued"

    ses.refuse = set()  # the blip clears before the first rung
    await escalation_dispatch.drain_background_retries()

    assert ses.recipients == [OWNER_EMAIL]
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"  # the owner heard; never "delivered"
    (item,) = await _items(admin)
    assert item.status == "delivered"
    assert item.last_error == SETTLED_OWNER_HEARD
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 0

    # The hourly sweep, the next hour and again past the item's TTL: nothing to replay,
    # nothing to abandon, nobody mailed, no backlog datum above zero.
    capsys.readouterr()
    for now in (datetime.now(UTC) + timedelta(hours=1), item.expires_at + timedelta(minutes=5)):
        assert await sweeps.run_deferred_retry(now=now) == []
    assert ses.recipients == [OWNER_EMAIL]
    assert await _abandons(admin) == []
    assert (await _row(admin, sid)).delivery_status == "owner_fallback"
    assert set(_backlog_data(capsys.readouterr().out)) == {0.0}
    assert len(await _audit(admin, "escalation_owner_fallback")) == 1


@pytest.mark.parametrize("contact", [None, "not-an-address"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_replay_with_nothing_left_to_retry_stands_down_and_completes_once(ses, contact):
    """The same zombie reached through the sweep: the item is still queued (its ladder
    died with the process, or it was filed before the rung settled items) while the row
    already reads ``owner_fallback``, and the configured contact offers nothing a retry
    could change — none at all, or one that can never be an address. The replay stands
    down from the owner, completes the item exactly once and pages nobody."""
    ses.refuse = None
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation={"primaryEmail": contact} if contact else {})

    await _fire(admin, iid, sid, lead)
    (item,) = await _items(admin)
    assert item.status == "queued" and item.payload["contacts_only"] is False
    # The owner heard through a pass that left the item queued.
    async with tenant_session(admin) as s:
        await s.execute(
            update(EscalationEvent)
            .where(EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid)
            .values(delivery_status="owner_fallback", delivery_detail="no_contact_configured")
        )

    ses.refuse = set()
    acted = await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1))

    assert acted == [str(admin)]
    (item,) = await _items(admin)
    assert item.status == "delivered"
    assert item.last_error == SETTLED_OWNER_HEARD
    assert item.attempts == 0  # completed, not retried-and-backed-off
    assert ses.recipients == []  # the owner is not paged a second time
    assert (await _row(admin, sid)).delivery_status == "owner_fallback"

    # Past the TTL the old code abandoned it: a terminal "delivery failed" audit row and
    # the "could not deliver an escalation alert" email. Neither happens now.
    assert await sweeps.run_deferred_retry(now=item.expires_at + timedelta(minutes=5)) == []
    assert await _abandons(admin) == []
    assert ses.recipients == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_contact_that_can_still_recover_keeps_its_retry(ses):
    """The stand-down is only for an item with nothing retryable: a contact the
    provider refused a moment ago is still tried on every pass, and its item stays
    queued until it lands or ages out."""
    ses.refuse = {CONTACT}
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation={"primaryEmail": CONTACT}, verified_emails=(CONTACT,))

    await _fire(admin, iid, sid, lead)
    assert ses.recipients == [OWNER_EMAIL]
    (item,) = await _items(admin)
    assert item.payload["contacts_only"] is True

    await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1))
    (item,) = await _items(admin)
    assert item.status == "queued" and item.attempts == 1
    assert item.last_error == "delivery_failed"

    ses.refuse = set()
    await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1))
    (item,) = await _items(admin)
    assert item.status == "delivered"
    assert ses.recipients == [OWNER_EMAIL, CONTACT]
    assert (await _row(admin, sid)).delivery_status == "delivered"


async def _age_row(admin: uuid.UUID, sid: uuid.UUID, age: timedelta) -> None:
    """The row was written this long ago (the sweep's candidate list and its bounds
    both read ``fired_at`` against the database clock)."""
    async with tenant_session(admin) as s:
        await s.execute(
            update(EscalationEvent)
            .where(EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid)
            .values(fired_at=datetime.now(UTC) - age)
        )


async def _age_send(admin: uuid.UUID, sid: uuid.UUID, age: timedelta) -> None:
    """The provider accepted the page this long ago (the receipt window's start)."""
    async with tenant_session(admin) as s:
        await s.execute(
            update(EscalationEvent)
            .where(EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid)
            .values(sent_at=datetime.now(UTC) - age)
        )


async def _age_page(admin: uuid.UUID, sid: uuid.UUID, age: timedelta) -> None:
    """The escalation fired AND its page went out this long ago."""
    await _age_row(admin, sid, age)
    await _age_send(admin, sid, age)


SMS_ESCALATION: dict[str, object] = {"primarySms": CONTACT_SMS, "preferredChannel": "sms"}


# --- a notification whose task died is the sweep's business ---------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_notification_whose_task_died_is_retried_by_the_sweep_and_counted(
    ses, monkeypatch, capsys
):
    """A deploy's shutdown cancels the off-turn notification task just after the
    customer's turn committed the escalation: the row stands at ``pending``, no page
    went out, nothing was queued. Nothing read ``pending`` — not the sweep, not the
    backlog, not the alarm — so the hot lead was dropped silently. Now a ``pending``
    row past the bound is counted in the backlog (and in /healthz's degraded entry
    once it outlives a sweep interval), and the hourly sweep audits the loss and
    retries it once through the normal path."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation={"primaryEmail": CONTACT}, verified_emails=(CONTACT,))

    real_notify = escalation_dispatch.notify_escalation_safely

    async def _cancelled_by_shutdown(*a, **kw):
        raise asyncio.CancelledError  # the lifespan's wait_for cancelling the gather

    monkeypatch.setattr(escalation_dispatch, "notify_escalation_safely", _cancelled_by_shutdown)
    await _fire(admin, iid, sid, lead)
    monkeypatch.setattr(escalation_dispatch, "notify_escalation_safely", real_notify)

    assert (await _row(admin, sid)).delivery_status == "pending"
    assert await _items(admin) == []
    assert ses.recipients == []
    # Young: the task may still be running — not a candidate, not in the backlog.
    assert await sweeps.run_deferred_retry() == []
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 0
    assert (await _row(admin, sid)).delivery_status == "pending"

    await _age_row(admin, sid, PENDING_STALE_AFTER + timedelta(hours=1))
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 1
    assert degraded_entry(backlog) == "escalation_backlog:1"

    capsys.readouterr()
    assert await sweeps.run_deferred_retry() == [str(admin)]
    assert ses.recipients == [CONTACT]
    assert (await _row(admin, sid)).delivery_status == "delivered"
    lost = [
        e
        for e in await _audit(admin, "escalation_delivery_failed")
        if e.event_payload.get("failure_reason") == NOTIFICATION_LOST
    ]
    assert len(lost) == 1 and lost[0].event_payload["session_id"] == str(sid)
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 0
    assert _backlog_data(capsys.readouterr().out) == [0.0]

    # Once: the next hour finds nothing to do and pages nobody again.
    assert await sweeps.run_deferred_retry() == []
    assert ses.recipients == [CONTACT]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_lost_notification_the_retry_cannot_deliver_joins_the_queue(ses, twilio):
    """The retry is the normal path: when the tenant's Twilio refuses it, the page is
    queued for the 24h ladder like any first attempt — the row leaves ``pending`` on
    the sweep's one pass whatever the provider says."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation=SMS_ESCALATION, confirmed_numbers=(CONTACT_SMS,))
    async with tenant_session(admin) as s:
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                resolved_lead_id=lead,
                signal="high_value_lead",
                gate="outcome",
                score_or_confidence=0.9,
                fired_at=datetime.now(UTC) - PENDING_STALE_AFTER - timedelta(minutes=1),
                delivery_status="pending",
            )
        )
    twilio.refuse = True
    ses.refuse = None  # the owner's email blips too

    assert await sweeps.run_deferred_retry() == [str(admin)]
    row = await _row(admin, sid)
    assert row.delivery_status == "queued"
    (item,) = await _items(admin)
    assert item.status == "queued" and item.payload["contacts_only"] is False
    assert twilio.sent == [] and ses.recipients == []


# --- a page whose receipt never came ------------------------------------------------- #
@pytest.mark.parametrize(
    ("answer", "status", "detail", "emails"),
    [
        (("delivered", None), "delivered", "sms:primary", []),
        (("undelivered", "30003"), "owner_fallback", "sms_dlr:undelivered:30003", [OWNER_EMAIL]),
    ],
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_page_whose_receipt_never_came_is_settled_by_one_status_fetch(
    ses, twilio, answer, status, detail, emails
):
    """Twilio accepted the page and the carrier's receipt never reached us (lost,
    blocked, or posted before the sending transaction committed): the row read
    ``sent`` forever and a carrier failure never reached the owner. Past the bound,
    the sweep asks the tenant's own Twilio for the message's status ONCE and stamps
    the answer exactly as the receipt would have."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation=SMS_ESCALATION, confirmed_numbers=(CONTACT_SMS,))
    await _fire(admin, iid, sid, lead)
    row = await _row(admin, sid)
    assert row.delivery_status == "sent" and row.delivery_provider_message_id
    message_sid = row.delivery_provider_message_id
    assert len(twilio.sent) == 1 and ses.recipients == []

    # Within the bound the receipt may still be on its way: nothing is asked.
    twilio.status = answer
    assert await sweeps.run_deferred_retry() == []
    assert twilio.fetched == []

    await _age_page(admin, sid, SENT_UNCONFIRMED_AFTER + timedelta(minutes=1))
    assert await sweeps.run_deferred_retry() == [str(admin)]
    assert twilio.fetched == [message_sid]
    row = await _row(admin, sid)
    assert row.delivery_status == status
    assert row.delivery_detail == detail
    assert ses.recipients == emails
    assert len(twilio.sent) == 1  # never paged again

    # Settled: the next sweep asks nothing and pages nobody.
    assert await sweeps.run_deferred_retry() == []
    assert twilio.fetched == [message_sid] and ses.recipients == emails


@pytest.mark.parametrize(
    ("answer", "raises", "why"),
    [
        (None, None, "status_unavailable"),
        (None, RuntimeError("twilio 500"), "status_unavailable"),
        (("sent", None), None, "status_sent"),
    ],
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_page_the_provider_cannot_settle_is_unconfirmed_and_the_owner_is_told(
    ses, twilio, answer, raises, why
):
    """No receipt, and the provider cannot be asked (or still has no terminal word):
    nobody can say the contact heard. The row is settled — never paged again — and
    the ladder continues to the owner, who is told; the row reads ``owner_fallback``
    with the honest reason. With no owner to reach it reads ``unconfirmed``."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation=SMS_ESCALATION, confirmed_numbers=(CONTACT_SMS,))
    await _fire(admin, iid, sid, lead)
    message_sid = (await _row(admin, sid)).delivery_provider_message_id
    await _age_page(admin, sid, SENT_UNCONFIRMED_AFTER + timedelta(minutes=1))
    twilio.status, twilio.fetch_raises = answer, raises

    assert await sweeps.run_deferred_retry() == [str(admin)]
    assert twilio.fetched == [message_sid]
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"
    assert row.delivery_detail == f"sms_unconfirmed:{why}"
    assert ses.recipients == [OWNER_EMAIL]
    (fallback,) = await _audit(admin, "escalation_owner_fallback")
    assert fallback.event_payload["reason"] == "sms_delivery_unconfirmed"
    (failed,) = await _audit(admin, "escalation_delivery_failed")
    assert failed.event_payload["failure_reason"] == f"sms_unconfirmed:{why}"
    assert failed.event_payload["provider_message_id"] == message_sid

    assert await sweeps.run_deferred_retry() == []
    assert ses.recipients == [OWNER_EMAIL] and len(twilio.sent) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_with_no_owner_to_reach_an_unsettled_page_reads_unconfirmed(ses, twilio):
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation=SMS_ESCALATION, confirmed_numbers=(CONTACT_SMS,))
    await _fire(admin, iid, sid, lead)
    await _age_page(admin, sid, SENT_UNCONFIRMED_AFTER + timedelta(minutes=1))
    ses.refuse = None  # the owner's mailbox is unreachable too

    assert await sweeps.run_deferred_retry() == [str(admin)]
    row = await _row(admin, sid)
    assert row.delivery_status == "unconfirmed"
    assert row.delivery_detail == "sms_unconfirmed:status_unavailable"
    assert ses.recipients == []
    assert await sweeps.run_deferred_retry() == []  # terminal: never asked again
    assert len(twilio.fetched) == 1


async def _make_due(admin: uuid.UUID) -> None:
    """The queued retry's backoff has run out (the hourly sweep finds it due)."""
    async with tenant_session(admin) as s:
        await s.execute(
            update(DeferredAction)
            .where(DeferredAction.admin_id == admin, DeferredAction.status == "queued")
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_page_the_sweep_itself_just_replayed_is_not_asked_about_in_the_same_pass(
    ses, twilio
):
    """The bound runs from the SEND, not from the escalation. The contact's first page
    and the owner's email both blipped, so the page was queued; the hourly sweep
    replays it an hour after the escalation fired and Twilio accepts it — the row reads
    ``sent``. The stale-``sent`` pass right after the replay, in the same sweep, read
    the row's age from ``fired_at``: it asked Twilio about a seconds-old message (still
    ``queued``), settled the row ``unconfirmed`` and emailed the owner that nobody could
    confirm a page the carrier was still delivering. The row now carries when it was
    sent, and the receipt gets its full window."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation=SMS_ESCALATION, confirmed_numbers=(CONTACT_SMS,))
    twilio.refuse, ses.refuse = True, None
    await _fire(admin, iid, sid, lead)
    assert (await _row(admin, sid)).delivery_status == "queued"
    await _age_row(admin, sid, timedelta(hours=1))
    await _make_due(admin)
    twilio.refuse, ses.refuse = False, set()
    twilio.status = ("queued", None)  # what Twilio says about a message seconds old

    assert await sweeps.run_deferred_retry() == [str(admin)]
    row = await _row(admin, sid)
    assert row.delivery_status == "sent" and row.delivery_detail == "sms:primary"
    assert row.sent_at is not None
    assert twilio.fetched == [] and ses.recipients == [] and len(twilio.sent) == 1
    (item,) = await _items(admin)
    assert item.status == "delivered"

    # The next hour: still inside the window counted from the send — nothing asked.
    assert await sweeps.run_deferred_retry() == []
    assert twilio.fetched == []

    # Past the window counted from the send, the one status fetch runs.
    await _age_send(admin, sid, SENT_UNCONFIRMED_AFTER + timedelta(minutes=1))
    twilio.status = ("delivered", None)
    assert await sweeps.run_deferred_retry() == [str(admin)]
    assert twilio.fetched == [row.delivery_provider_message_id]
    assert (await _row(admin, sid)).delivery_status == "delivered"
    assert ses.recipients == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_lost_notification_retried_by_text_is_not_asked_about_in_the_same_pass(ses, twilio):
    """The same hole on the lost-``pending`` path: a notification lost half an hour ago
    is retried by the sweep through the tenant's Twilio, and the page it just sent must
    not be judged stale by the escalation's age a moment later."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation=SMS_ESCALATION, confirmed_numbers=(CONTACT_SMS,))
    async with tenant_session(admin) as s:
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                resolved_lead_id=lead,
                signal="high_value_lead",
                gate="outcome",
                score_or_confidence=0.9,
                fired_at=datetime.now(UTC) - timedelta(minutes=30),
                delivery_status="pending",
            )
        )
    twilio.status = ("queued", None)

    assert await sweeps.run_deferred_retry() == [str(admin)]
    row = await _row(admin, sid)
    assert row.delivery_status == "sent" and row.sent_at is not None
    assert len(twilio.sent) == 1 and twilio.fetched == [] and ses.recipients == []


# --- the owner is told once ---------------------------------------------------------- #
async def _receipt(admin: uuid.UUID, iid: uuid.UUID, message_sid: str, status: str) -> str:
    async with tenant_session(admin) as s:
        return await escalation_dispatch.record_sms_delivery_receipt(
            s,
            admin_id=admin,
            instance_id=iid,
            provider_message_id=message_sid,
            message_status=status,
            error_code="30003" if status != "delivered" else None,
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_receipt_after_the_cc_fan_out_does_not_email_the_owner_twice(ses, twilio):
    """A routing rule with ccOwnerEmail and a confirmed SMS contact: the page is
    accepted and the owner gets the cc email. The carrier then reports undelivered —
    the owner used to receive an identical second email, because only the
    unverified-contact copy left the ``+owner`` marker on the row."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(
        admin,
        iid,
        escalation={
            **SMS_ESCALATION,
            "routing": [{"signal": "high_value_lead", "channel": "sms", "ccOwnerEmail": True}],
        },
        confirmed_numbers=(CONTACT_SMS,),
    )
    await _fire(admin, iid, sid, lead)
    row = await _row(admin, sid)
    assert row.delivery_status == "sent"
    assert row.delivery_detail == "sms:primary+owner"
    assert ses.recipients == [OWNER_EMAIL]  # the cc
    message_sid = row.delivery_provider_message_id
    assert message_sid

    assert await _receipt(admin, iid, message_sid, "undelivered") == "owner_fallback"
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"
    assert row.delivery_detail == "sms_dlr:undelivered:30003"
    assert ses.recipients == [OWNER_EMAIL]  # once
    assert await _audit(admin, "escalation_owner_fallback") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_owner_fallback_then_a_replayed_page_that_fails_does_not_email_the_owner_twice(
    ses, twilio
):
    """Twilio's API refuses the first attempt, so the owner gets the fallback email and
    the contact's retry is queued contacts-only. The hourly replay is accepted by the
    API — which re-stamped the row ``sent`` / ``sms:primary`` over the
    ``owner_fallback`` that said the owner heard — and the carrier then fails it: the
    owner was emailed again. The replay keeps that fact on the row now."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation=SMS_ESCALATION, confirmed_numbers=(CONTACT_SMS,))
    twilio.refuse = True
    await _fire(admin, iid, sid, lead)
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"
    assert ses.recipients == [OWNER_EMAIL]
    (item,) = await _items(admin)
    assert item.status == "queued" and item.payload["contacts_only"] is True

    twilio.refuse = False
    assert await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1)) == [
        str(admin)
    ]
    (item,) = await _items(admin)
    assert item.status == "delivered"
    row = await _row(admin, sid)
    assert row.delivery_status == "sent"
    assert row.delivery_detail == "sms:primary+owner"
    message_sid = row.delivery_provider_message_id
    assert message_sid and len(twilio.sent) == 1

    assert await _receipt(admin, iid, message_sid, "failed") == "owner_fallback"
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"
    assert ses.recipients == [OWNER_EMAIL]  # once
    assert len(await _audit(admin, "escalation_owner_fallback")) == 1  # the first pass only


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_cc_that_reached_the_owner_is_the_fallback_so_replays_never_cc_again(ses, twilio):
    """ccOwnerEmail with a contact whose page Twilio refused: the cc reached the owner,
    but the row was queued as if nobody had heard, with a retry that was NOT
    contacts-only — so every replay (the ladder's rungs, each hourly sweep) sent the
    owner the cc again before trying the contact. The cc that landed is the owner
    hearing: the row reads ``owner_fallback`` and the retry is for the contact alone."""
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(
        admin,
        iid,
        escalation={
            **SMS_ESCALATION,
            "routing": [{"signal": "high_value_lead", "channel": "sms", "ccOwnerEmail": True}],
        },
        confirmed_numbers=(CONTACT_SMS,),
    )
    twilio.refuse = True
    await _fire(admin, iid, sid, lead)
    assert ses.recipients == [OWNER_EMAIL]  # the cc, once
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"
    (item,) = await _items(admin)
    assert item.status == "queued" and item.payload["contacts_only"] is True

    # The hourly replay: still refused, then accepted — the owner is never cc'd again.
    await _make_due(admin)
    assert await sweeps.run_deferred_retry() == [str(admin)]
    assert ses.recipients == [OWNER_EMAIL]
    twilio.refuse = False
    await _make_due(admin)
    assert await sweeps.run_deferred_retry() == [str(admin)]
    row = await _row(admin, sid)
    assert row.delivery_status == "sent" and row.delivery_detail == "sms:primary+owner"
    assert ses.recipients == [OWNER_EMAIL] and len(twilio.sent) == 1
