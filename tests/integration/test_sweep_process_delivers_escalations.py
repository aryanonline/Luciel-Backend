"""The hourly session_finalize PROCESS pages the owner about a stalled booking
(round 7 phase 2 review, seams 2 — and the judgment finding "the session-close
'booking stalled' escalation writes its row but the notification is cancelled when
the sweep process exits").

``tests/integration/test_round7_session_close.py`` proves the close writes the
``cannot_answer`` row. It calls ``finalize_due_sessions`` inside the test's own
long-lived loop, where the suite's teardown drain finishes the notification — which
is exactly what the production path did not have.

Here the real sweep runs through ``app.jobs.__main__._run_sweep``, the coroutine
``python -m app.jobs session_finalize`` hands to ``asyncio.run``. ``asyncio.run``
cancels every task still pending at the instant that coroutine returns, so the test
looks at that instant: with no await and no drain of its own in between, nothing may
still be registered as running, and the page must already be on record.
(``tests/unit/test_sweep_entrypoint_drains_notifications.py`` drives ``main`` itself
through a real ``asyncio.run`` exit, with a stand-in sweep.)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.jobs import __main__ as jobs_main
from app.jobs.sweeps import SESSION_FINALIZE
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as ConvSession
from app.models.email_incident import EmailDeliveryIncident
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.trace import ToolTraceEvent
from app.notifications import email_notifier, escalation_dispatch
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

OWNER_EMAIL = "owner@acme.example"
CONTACT = "sales@acme.example"


class _RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        self.sent.append((to, body_text))
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch: pytest.MonkeyPatch) -> _RecordingEmailSender:
    fake = _RecordingEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _seed_stalled_booking(admin: uuid.UUID, iid: uuid.UUID) -> uuid.UUID:
    """An SMS conversation where the broker refused book_appointment for want of a
    phone number, Luciel asked, and the customer went quiet nine hours ago (the SMS
    inactivity window is four) — the session only the sweep can close."""
    sid = uuid.uuid4()
    last = datetime.now(UTC) - timedelta(hours=9)
    async with tenant_session(admin) as s:
        s.add(
            EmailDeliveryIncident(
                admin_id=admin, email_lower=CONTACT, verified_at=datetime.now(UTC)
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
                escalation={"primaryEmail": CONTACT, "preferredChannel": "email"},
            )
        )
        s.add(
            ConvSession(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="sms",
                mode="ai",
                started_at=last - timedelta(minutes=5),
                last_message_at=last,
                input_ask_attempts=1,
            )
        )
        await s.flush()
        s.add(
            ToolTraceEvent(
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                tool_id="book_appointment",
                outcome="missing_input",
                connection_type="calendar",
                missing_fields=["phone"],
                at=last,
            )
        )
        s.add(
            Transcript(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="lead",
                text="can I book Thursday at 2?",
                at=last,
            )
        )
        await s.flush()
    return sid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_process_pages_the_owner_before_it_exits(
    ses: _RecordingEmailSender,
) -> None:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed_stalled_booking(admin, iid)

    acted = await jobs_main._run_sweep(SESSION_FINALIZE)
    # The process exits HERE. Anything still registered is what asyncio.run cancels,
    # and a page not yet sent is a page nobody gets: both are checked before this
    # test awaits anything else.
    still_running = list(escalation_dispatch._BACKGROUND_NOTIFICATIONS)
    sent_at_exit = [to for to, _ in ses.sent]
    assert still_running == []
    assert sent_at_exit == [CONTACT]
    assert acted == [str(admin)]

    async with tenant_session(admin) as s:
        sess = (await s.execute(select(ConvSession).where(ConvSession.id == sid))).scalar_one()
        row = (
            await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid))
        ).scalar_one()
        sent_events = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "escalation_notification_sent",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sess.ended_at is not None
    assert row.signal == "cannot_answer" and row.gate == "outcome"
    assert "still missing phone" in list(row.reasons or [])
    # The page left with the process still alive to send it: the row is stamped, the
    # send is on record, and it went to the configured contact — not "pending" forever.
    assert row.delivery_status == "delivered", row.delivery_status
    assert row.delivered_at is not None
    # ... and it says what stalled, not merely that something did.
    assert "still missing phone" in ses.sent[0][1]
    assert "the conversation ended before the booking could be completed" in ses.sent[0][1]
    assert len(sent_events) == 1
