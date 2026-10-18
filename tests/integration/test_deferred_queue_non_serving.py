"""Async work while the Luciel is paused or deleted (round 7 WP-5, item 2).

Live condition: ``deferred_queue.run_sweep`` never consulted the instance state, so a
PAUSED Luciel still sent scheduled callbacks — "Pausing the Luciel pauses every
channel" (root CLAUDE.md) was true of the turn path and false of the sweep. Now a
lead-facing item is HELD while the Luciel is paused (resume drains it) or in its 30-day
grace window (a Restore or the D5 reopen drains it — round 7 money review, finding 3),
and a HARD-deleted Luciel lets it age out to its TTL with the owner's abandon email
suppressed. Owner-facing escalation retries are untouched. The export build is allowed
in every state — it is the export offer of the grace window and a paused owner's right
just the same.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from app.connections import deferred_queue
from app.db.session import tenant_session
from app.lifecycle import cascade, reopen
from app.lifecycle.export_bundle import build_export_zip
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from app.models.notice_delivery import NoticeDelivery
from app.notifications import email_dispatcher, escalation_dispatch
from sqlalchemy import select

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


async def _seed(admin: uuid.UUID, *, state: LucielState) -> tuple[uuid.UUID, uuid.UUID]:
    iid, lead_id = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
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
                state=state,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": "email", "enabled": True}],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="ses",
                status="connected",
            )
        )
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Jane",
                contact_identifier="jane@lead.example",
                opted_out=False,
            )
        )
    return iid, lead_id


class _Recording:
    def __init__(self) -> None:
        self.delivered: list[uuid.UUID] = []

    async def deliver(self, action: DeferredAction) -> bool:
        self.delivered.append(action.id)
        return True


async def _enqueue_callback(admin: uuid.UUID, iid: uuid.UUID, lead_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="schedule_callback",
            connection_type="email_sender",
            resolved_lead_id=lead_id,
            payload={"fire_at": NOW.isoformat(), "channel": "email", "message": "Still keen?"},
            now=NOW,
        )
        return action.id


async def _action(admin: uuid.UUID, action_id: uuid.UUID) -> DeferredAction:
    async with tenant_session(admin) as s:
        return (
            await s.execute(select(DeferredAction).where(DeferredAction.id == action_id))
        ).scalar_one()


async def _set_state(admin: uuid.UUID, state: LucielState) -> None:
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.admin_id == admin))).scalar_one()
        inst.state = state


async def _sweep(admin: uuid.UUID, deliverer: _Recording, *, at: datetime) -> dict[str, int]:
    async with tenant_session(admin) as s:
        return await deferred_queue.run_sweep(s, admin_id=admin, deliverer=deliverer, now=at)


@pytest.fixture
def abandon_emails(monkeypatch) -> list[dict[str, object]]:
    sent: list[dict[str, object]] = []

    async def record(session, **kwargs):
        sent.append(kwargs)
        return email_dispatcher.DeliveryOutcome(status="recorded")

    monkeypatch.setattr(email_dispatcher, "send_deferred_abandon_notice", record)
    return sent


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_paused_luciel_holds_the_callback_and_resume_drains_it(abandon_emails):
    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, state=LucielState.paused)
    action_id = await _enqueue_callback(admin, iid, lead_id)
    deliverer = _Recording()

    counts = await _sweep(admin, deliverer, at=NOW + timedelta(minutes=1))
    assert counts == {"delivered": 0, "abandoned": 0, "retried": 0, "held": 1}
    assert deliverer.delivered == [], "a paused Luciel sends nothing"
    held = await _action(admin, action_id)
    assert held.status == "queued"
    assert held.last_error == deferred_queue.LUCIEL_NOT_SERVING
    assert held.next_attempt_at == NOW + timedelta(minutes=1) + deferred_queue.HOLD_RECHECK

    # Paused for two days: still held, never abandoned, no owner email.
    two_days_on = NOW + timedelta(days=2)
    counts = await _sweep(admin, deliverer, at=two_days_on)
    assert counts["held"] == 1 and counts["abandoned"] == 0
    assert (await _action(admin, action_id)).expires_at >= two_days_on + deferred_queue.MAX_AGE
    assert abandon_emails == []

    # Resume: the next sweep delivers it.
    await _set_state(admin, LucielState.active)
    counts = await _sweep(admin, deliverer, at=two_days_on + deferred_queue.HOLD_RECHECK)
    assert counts["delivered"] == 1
    assert deliverer.delivered == [action_id]
    assert (await _action(admin, action_id)).status == "delivered"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_grace_window_luciel_holds_the_callback_and_restore_drains_it(abandon_emails):
    """Round 7 money review, finding 3. The grace window aged a lead's callback out
    like a final deletion (abandoned at 24h, owner email suppressed), so a Luciel
    restored on day 3 owed a lead a call it would never make. A Luciel that can still
    come back holds its lead-facing work exactly as a paused one does."""
    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, state=LucielState.luciel_grace_window)
    action_id = await _enqueue_callback(admin, iid, lead_id)
    deliverer = _Recording()

    counts = await _sweep(admin, deliverer, at=NOW + timedelta(hours=1))
    assert counts == {"delivered": 0, "abandoned": 0, "retried": 0, "held": 1}
    assert deliverer.delivered == [], "a deleted Luciel sends nothing"

    # Two days in the grace window: still held, never abandoned, no owner email.
    two_days_on = NOW + timedelta(days=2)
    counts = await _sweep(admin, deliverer, at=two_days_on)
    assert counts["held"] == 1 and counts["abandoned"] == 0
    held = await _action(admin, action_id)
    assert held.status == "queued"
    assert held.last_error == deferred_queue.LUCIEL_NOT_SERVING
    assert held.expires_at >= two_days_on + deferred_queue.MAX_AGE
    assert abandon_emails == []

    # Restored: the next sweep delivers it.
    await _set_state(admin, LucielState.active)
    counts = await _sweep(admin, deliverer, at=two_days_on + deferred_queue.HOLD_RECHECK)
    assert counts["delivered"] == 1
    assert deliverer.delivered == [action_id]
    assert (await _action(admin, action_id)).status == "delivered"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_hard_deleted_luciel_lets_the_callback_age_out_without_the_owner_email(
    abandon_emails,
):
    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, state=LucielState.luciel_hard_deleted)
    action_id = await _enqueue_callback(admin, iid, lead_id)
    deliverer = _Recording()

    counts = await _sweep(admin, deliverer, at=NOW + timedelta(hours=1))
    assert counts["held"] == 1 and counts["delivered"] == 0 and counts["abandoned"] == 0
    assert deliverer.delivered == []
    assert (await _action(admin, action_id)).status == "queued"

    # Past the TTL: abandoned and audited, but the owner is not emailed about a
    # follow-up on a Luciel they deleted — not now, not by the 48h catch-up pass.
    counts = await _sweep(admin, deliverer, at=NOW + timedelta(hours=25))
    assert counts["abandoned"] == 1
    assert deliverer.delivered == []
    aged = await _action(admin, action_id)
    assert aged.status == "abandoned"
    assert aged.last_error == deferred_queue.LUCIEL_DELETED
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_status_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.event_payload["action"] for r in rows] == ["deferred_abandoned"]
        assert "deleted" in rows[0].event_payload["reason"]
        ledger = (
            (await s.execute(select(NoticeDelivery).where(NoticeDelivery.admin_id == admin)))
            .scalars()
            .all()
        )
        assert ledger == []
    assert abandon_emails == []
    await _sweep(admin, deliverer, at=NOW + timedelta(hours=30))
    assert abandon_emails == [], "the catch-up pass does not resurrect the suppressed email"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_callback_queued_before_a_closure_is_delivered_after_the_reopen(abandon_emails):
    """The finding's live path, end to end: a lead asks for a callback, the owner closes
    the account that evening (the closure deletes the Luciel into its grace window) and
    reopens two days later (D5: "exactly as it was"). The callback used to be abandoned
    at its 24h TTL as ``luciel_deleted`` with the owner email suppressed; it is held
    through the closure and the first sweep after the reopen delivers it."""
    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, state=LucielState.active)
    t0 = datetime.now(UTC)
    async with tenant_session(admin) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="schedule_callback",
            connection_type="email_sender",
            resolved_lead_id=lead_id,
            payload={"fire_at": t0.isoformat(), "channel": "email", "message": "Still keen?"},
            now=t0,
        )
        action_id = action.id
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    deliverer = _Recording()

    assert (await _sweep(admin, deliverer, at=t0 + timedelta(hours=1)))["held"] == 1
    two_days_on = t0 + timedelta(days=2)
    counts = await _sweep(admin, deliverer, at=two_days_on)
    assert counts["held"] == 1 and counts["abandoned"] == 0
    assert deliverer.delivered == [], "a closed account's Luciel sends nothing"
    assert (await _action(admin, action_id)).status == "queued"

    async with tenant_session(admin) as s:
        outcome = await reopen.reopen_account(s, admin, admin, now=two_days_on)
    assert (outcome.luciel_restored, outcome.luciel_state) == (True, "active")

    counts = await _sweep(admin, deliverer, at=two_days_on + deferred_queue.HOLD_RECHECK)
    assert counts["delivered"] == 1
    assert deliverer.delivered == [action_id]
    assert (await _action(admin, action_id)).status == "delivered"
    async with tenant_session(admin) as s:
        abandons = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_status_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r for r in abandons if r.event_payload.get("action") == "deferred_abandoned"] == []
    assert abandon_emails == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_owner_facing_escalation_retry_still_goes_out_while_paused():
    """The owner of a paused Luciel still hears about the lead it escalated before
    the pause — only LEAD-facing work is held."""
    admin = uuid.uuid4()
    iid, _lead_id = await _seed(admin, state=LucielState.paused)
    async with tenant_session(admin) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type=escalation_dispatch.ESCALATION_RETRY_ACTION,
            connection_type=deferred_queue.PLATFORM_NOTIFICATION,
            payload={"escalation_event_id": str(uuid.uuid4())},
            now=NOW,
        )
        action_id = action.id
    deliverer = _Recording()
    counts = await _sweep(admin, deliverer, at=NOW + timedelta(minutes=1))
    assert counts["delivered"] == 1 and counts["held"] == 0
    assert deliverer.delivered == [action_id]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_active_luciel_delivers_as_before():
    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, state=LucielState.active)
    action_id = await _enqueue_callback(admin, iid, lead_id)
    deliverer = _Recording()
    counts = await _sweep(admin, deliverer, at=NOW + timedelta(minutes=1))
    assert counts["delivered"] == 1 and counts["held"] == 0
    assert deliverer.delivered == [action_id]


@pytest.mark.parametrize("state", [LucielState.paused, LucielState.luciel_grace_window])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_export_builds_for_a_non_serving_luciel(state):
    """The export is the grace window's offer and a paused owner's right alike: the
    build has no state gate, and the bundle carries the instance as it stands."""
    admin = uuid.uuid4()
    await _seed(admin, state=state)
    async with tenant_session(admin) as s:
        data = await build_export_zip(s, admin)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        assert {"README.txt", "instance.json", "leads.json", "audit_log.jsonl"} <= names
        assert '"name": "Acme Patios"' in z.read("instance.json").decode()
