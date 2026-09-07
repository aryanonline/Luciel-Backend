"""Lifecycle state machine + cascade integration tests (Architecture §3.6).

Run against the real DB with RLS. Cover: pause/resume, fused-quiesce delete →
grace window, restore → ACTIVE (not paused), the ordered hard-delete cascade
(account shell + audit log survive), and account closure.
"""

from __future__ import annotations

import uuid

import pytest
from app.audit.log import verify_chain
from app.db.session import tenant_session
from app.lifecycle import cascade, state_machine
from app.lifecycle.state_machine import LifecycleError
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeSource
from app.models.lead import Lead
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]


async def _seed_account_with_luciel(admin_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin_id) as s:
        acct = Account(
            id=admin_id,
            admin_id=admin_id,
            user_id=admin_id,
            email=f"{admin_id}@x.com",
            email_lower=f"{admin_id}@x.com",
            password_hash="x",
            state=AccountState.verified,
            email_verified=True,
        )
        s.add(acct)
        inst = Instance(
            id=uuid.uuid4(),
            admin_id=admin_id,
            name="L",
            state=LucielState.active,
            embed_key_public_id="vm_live_seed",
            channels=[],
            tools=[],
            escalation={},
            personality={},
        )
        s.add(inst)
        s.add(Lead(id=uuid.uuid4(), admin_id=admin_id, instance_id=inst.id, name="Lead"))
        s.add(
            KnowledgeSource(
                id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=inst.id,
                name="f.pdf",
                origin="upload",
                ingestion_status="ready",
            )
        )
        await s.flush()
        return inst.id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_pause_resume_roundtrip():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        inst = await state_machine.pause(s, admin, admin)
        assert inst.state == LucielState.paused
    async with tenant_session(admin) as s:
        inst = await state_machine.resume(s, admin, admin)
        assert inst.state == LucielState.active


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_fused_quiesce_to_grace_then_restore_active():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    # Delete from ACTIVE (no manual pause-first).
    async with tenant_session(admin) as s:
        inst = await state_machine.delete_luciel(s, admin, admin)
        assert inst.state == LucielState.luciel_grace_window
        assert inst.grace_window_started_at is not None
        # C8: the key STAYS live through grace — the resolver admits grace-window
        # instances so an in-flight conversation can drain (§3.6.3 quiesce) while
        # the channel gates refuse NEW sessions. A stashed copy still backs
        # restore's same-key revival for pre-0048 rows.
        assert inst.embed_key_public_id == "vm_live_seed"
        assert inst.embed_key_stashed == "vm_live_seed"
    # Knowledge soft-deleted during quiesce.
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        assert src.soft_deleted_at is not None
    # Restore → ACTIVE (not paused), and the SAME embed key revives (§3.6.4
    # "exactly as it was") — a fresh key would permanently kill the snippet
    # already deployed on the customer's website.
    async with tenant_session(admin) as s:
        inst = await state_machine.restore(s, admin, admin)
        assert inst.state == LucielState.active
        assert inst.grace_window_started_at is None
        assert inst.embed_key_public_id == "vm_live_seed"
        assert inst.embed_key_stashed is None
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        assert src.soft_deleted_at is None


async def test_restore_of_a_legacy_grace_row_with_no_stash_mints_a_fresh_key() -> None:
    """A row that entered grace before the stash column existed has nothing to
    revive — restore falls back to minting, never crashes and never restores None."""
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, admin)
        inst = (await s.execute(select(Instance).where(Instance.admin_id == admin))).scalar_one()
        # Simulate the pre-0039 grace row: key nulled at delete (old behavior)
        # AND no stash column value.
        inst.embed_key_public_id = None
        inst.embed_key_stashed = None
    async with tenant_session(admin) as s:
        inst = await state_machine.restore(s, admin, admin)
        assert inst.state == LucielState.active
        assert inst.embed_key_public_id and inst.embed_key_public_id.startswith("vm_live_")
        assert inst.embed_key_public_id != "vm_live_seed"


async def test_delete_schedules_secret_cleanup_rather_than_claiming_it_happened() -> None:
    """The delete-time audit event must not assert a cleanup that has not run: the
    secret survives the 30-day grace (restore needs it), so delete emits
    connection_secret_cleanup_scheduled; the real connection_secret_cleanup belongs
    to the cascade/disconnect paths that actually destroy material."""
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=(
                    await s.execute(select(Instance.id).where(Instance.admin_id == admin))
                ).scalar_one(),
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google_calendar",
                status="connected",
                secret_ref="vm/keep-me",
            )
        )
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, admin)
    async with tenant_session(admin) as s:
        events = (
            (await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin)))
            .scalars()
            .all()
        )
        kinds = {e.event_type for e in events}
        assert "connection_secret_cleanup_scheduled" in kinds
        assert "connection_secret_cleanup" not in kinds  # nothing was destroyed yet
        scheduled = [e for e in events if e.event_type == "connection_secret_cleanup_scheduled"]
        assert scheduled[0].event_payload["secret_ref_present"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_allowed_from_paused():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        await state_machine.pause(s, admin, admin)
    async with tenant_session(admin) as s:
        inst = await state_machine.delete_luciel(s, admin, admin)
        assert inst.state == LucielState.luciel_grace_window


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_only_from_grace_window():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        with pytest.raises(LifecycleError):
            await state_machine.restore(s, admin, admin)  # active, not in grace


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hard_delete_cascade_preserves_account_shell_and_audit():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, admin)
    async with tenant_session(admin) as s:
        counts = await cascade.hard_delete_cascade(s, admin)
        assert counts["leads"] >= 1
        assert counts["knowledge_sources"] >= 1
    # Instance + leads gone; account shell + audit log survive; chain intact.
    async with tenant_session(admin) as s:
        assert (await s.execute(select(func.count()).select_from(Instance))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(Lead))).scalar_one() == 0
        acct = (await s.execute(select(Account).where(Account.id == admin))).scalar_one()
        assert acct is not None  # shell survives
        audit_count = (
            await s.execute(select(func.count()).select_from(AdminAuditLog))
        ).scalar_one()
        assert audit_count > 0  # tombstones retained (proof of deletion)
        assert await verify_chain(s, admin) is True  # hash chain intact through deletions


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_close_account_deletes_luciel_first_then_closes():
    """Closure subsumes deletion (§3.6.6) — the SAME deletion every owner gets, with its
    30-day grace window (audit F001), not a synchronous hard-delete the copy never promised."""
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    async with tenant_session(admin) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin))).scalar_one()
        assert acct.state == AccountState.closed
        inst = (await s.execute(select(Instance))).scalar_one()
        assert inst.state == LucielState.luciel_grace_window
        assert inst.grace_window_started_at is not None
        # The Luciel's data is still there for the window: restorable, not destroyed.
        assert (await s.execute(select(func.count()).select_from(Lead))).scalar_one() == 1
        assert await verify_chain(s, admin) is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_close_account_settles_outstanding_usage_while_the_card_is_on_file():
    """Legal §A4 / audit F002: the metered subscription ends with a final invoice for
    the partial period, and the accrued residual is flushed first — closing used to
    abandon the subscription and forgive the last blocks."""
    from app.billing import metering
    from app.billing.stripe_gateway import FakeStripeGateway
    from app.models.billing import BillingAccount

    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    gw = FakeStripeGateway()
    gw.seed_customer("cus_closing", subscription_id="sub_closing")
    async with tenant_session(admin) as s:
        s.add(
            BillingAccount(
                admin_id=admin,
                billing_state="payg_enabled",
                stripe_customer_id="cus_closing",
                stripe_subscription_id="sub_closing",
                accrued_blocks=3,
                last_reported_blocks=2,
            )
        )
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin, gateway=gw)
    assert [e.value for e in gw.meter_events] == [1], "the residual block was billed"
    assert gw.cancelled_subscriptions == [("sub_closing", True)]
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.stripe_subscription_id is None
        assert acct.stripe_customer_id == "cus_closing"  # retired at purge, not at close
        settled = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "billing_closure_settled",
                )
            )
        ).scalar_one()
        assert settled.event_payload["subscription_cancelled"] is True
        assert settled.event_payload["usage_owed"] is True
        # R-WP1-3: the flush succeeded, so nothing is left unreported.
        assert settled.event_payload["usage_unreported"] is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_grace_expiry_keeps_the_billing_identity_and_purge_retires_it():
    """F014/F019: a hard-deleted Luciel leaves an account that still owns its card, its
    lifetime count and its Stripe customer (the owner may build a new Luciel). Only the
    account purge destroys that, and it deletes the Stripe customer first."""
    from datetime import UTC, datetime, timedelta

    from app.billing.stripe_gateway import FakeStripeGateway, set_stripe_gateway
    from app.lifecycle import close_purge
    from app.models.billing import BillingAccount

    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        s.add(
            BillingAccount(admin_id=admin, stripe_customer_id="cus_keep", lifetime_conversations=77)
        )
    async with tenant_session(admin) as s:
        await cascade.hard_delete_cascade(s, admin)  # the day-30 Luciel cascade
    async with tenant_session(admin) as s:
        acct = (await s.execute(select(BillingAccount))).scalar_one()
        assert acct.lifetime_conversations == 77
        assert acct.stripe_customer_id == "cus_keep"

    gw = FakeStripeGateway()
    gw.seed_customer("cus_keep")
    set_stripe_gateway(gw)
    try:
        async with tenant_session(admin) as s:
            await cascade.close_account(s, admin, admin, gateway=gw)
        async with tenant_session(admin) as s:
            purged = await close_purge.purge_closed_account_if_due(
                s, admin_id=admin, now=datetime.now(UTC) + timedelta(days=31)
            )
        assert purged is True
    finally:
        set_stripe_gateway(None)
    assert gw.deleted_customers == ["cus_keep"]
    async with tenant_session(admin) as s:
        assert (await s.execute(select(BillingAccount))).scalar_one_or_none() is None
        retired = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "stripe_customer_deleted",
                )
            )
        ).scalar_one()
        assert retired.event_payload == {
            "customer_id": "cus_keep",
            "subscription_id": None,
            "deleted": True,
            "error": None,
            "context": "hard_delete_cascade",
        }


# ---------------------------------------------------------------------------
# Regression tests for Axis3-F02 (hard-delete cascade calls SecretStore.delete)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hard_delete_cascade_calls_secret_store_delete():
    """Axis3-F02: hard_delete_cascade must call SecretStore.delete() for each connection."""
    import uuid as _uuid

    from app.connections.secrets import LocalSecretStore, secret_ref_for
    from app.lifecycle.cascade import hard_delete_cascade
    from app.models.connection import InstanceConnection

    admin = _uuid.uuid4()
    store = LocalSecretStore()
    # Patch the global store so the cascade uses our test store.
    import app.connections.secrets as _sec

    original = _sec._default
    _sec._default = store

    try:
        async with tenant_session(admin) as s:
            s.add(
                Account(
                    id=admin,
                    admin_id=admin,
                    user_id=admin,
                    email=f"{admin}@cs.test",
                    email_lower=f"{admin}@cs.test",
                    password_hash="x",
                    state=AccountState.verified,
                    email_verified=True,
                )
            )
            iid = _uuid.uuid4()
            s.add(
                Instance(
                    id=iid,
                    admin_id=admin,
                    name="T",
                    state=LucielState.luciel_grace_window,
                    channels=[],
                    tools=[],
                    escalation={},
                    personality={},
                )
            )
            ref = secret_ref_for(admin, iid, "calendar")
            s.add(
                InstanceConnection(
                    connection_id=_uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="calendar",
                    auth_class="oauth_token",
                    provider="google",
                    status="connected",
                    secret_ref=ref,
                )
            )
            await s.flush()

        # Seed a secret into the store.
        await store.put(ref, {"access_token": "tok-abc"})
        assert await store.get(ref) is not None, "precondition: secret must be in store"

        async with tenant_session(admin) as s:
            await hard_delete_cascade(s, admin)

        # Secret must have been deleted by the cascade.
        assert await store.get(ref) is None, (
            "SecretStore.delete() must be called during hard_delete_cascade (§3.6.5 step 6)"
        )
    finally:
        _sec._default = original


# ---------------------------------------------------------------------------
# Regression tests for Axis3-F03 (lead_reactivated event)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_archived_lead_reactivated_on_inbound():
    """Axis3-F03: Returning archived lead is reactivated with lead_reactivated event."""
    from app.models.audit import AdminAuditLog
    from app.models.lead import LeadState
    from app.runtime.sessions import resolve_or_open_session

    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@lr.test",
                email_lower=f"{admin}@lr.test",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="T",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        lead_id = uuid.uuid4()
        from app.models.lead import Lead

        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Test User",
                contact_identifier="test@x.com",
                state=LeadState.archived,
            )
        )
        await s.flush()

    async with tenant_session(admin) as s:
        sess, is_new = await resolve_or_open_session(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="email",
            resolved_lead_id=lead_id,
        )

    # Lead must now be active
    async with tenant_session(admin) as s:
        from app.models.lead import Lead as LeadModel
        from sqlalchemy import select as sa_sel

        lead = (await s.execute(sa_sel(LeadModel).where(LeadModel.id == lead_id))).scalar_one()
        assert lead.state == LeadState.active, (
            f"Archived lead must be reactivated on inbound; got {lead.state!r}"
        )

        # lead_reactivated audit event must be emitted
        evt = (
            await s.execute(
                sa_sel(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "lead_reactivated",
                )
            )
        ).scalar_one_or_none()
        assert evt is not None, "lead_reactivated audit event must be emitted"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_grace_expiry_sweep_fires_cascade_only_after_30_days():
    """Axis3-F06: the day-30 grace-window expiry job fires the hard-delete cascade.

    Before day 30 the Luciel is recoverable and the sweep is a no-op; after the
    deadline the sweep runs the cascade (and emits the grace_window_expiry
    lifecycle_transition), honoring the §3.6.3 'then permanently destroyed' promise.
    """
    from datetime import timedelta

    from app.lifecycle.grace_expiry import expire_grace_window_if_due, run_grace_expiry_sweep

    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    # Move into the grace window.
    async with tenant_session(admin) as s:
        inst = await state_machine.delete_luciel(s, admin, admin)
        started = inst.grace_window_started_at
        assert started is not None

    # 29 days in: NOT yet due — sweep is a no-op, data still present.
    not_yet = started + timedelta(days=29)
    async with tenant_session(admin) as s:
        assert await expire_grace_window_if_due(s, admin_id=admin, now=not_yet) is None
    async with tenant_session(admin) as s:
        n = (
            await s.execute(select(func.count()).select_from(Lead).where(Lead.admin_id == admin))
        ).scalar_one()
        assert n >= 1, "data must remain recoverable before day 30"

    # Day 30 + 1s: due — the multi-tenant sweep deletes this tenant's Luciel.
    past = started + timedelta(days=30, seconds=1)
    deleted = await run_grace_expiry_sweep([admin, uuid.uuid4()], now=past)
    assert str(admin) in deleted, "expired tenant must be cascaded"

    async with tenant_session(admin) as s:
        n = (
            await s.execute(select(func.count()).select_from(Lead).where(Lead.admin_id == admin))
        ).scalar_one()
        assert n == 0, "leads must be hard-deleted after grace expiry"
        # Audit trail of the expiry transition is retained (audit log survives cascade).
        evt = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "lifecycle_transition",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any((e.event_payload or {}).get("trigger") == "grace_window_expiry" for e in evt), (
            "grace_window_expiry lifecycle_transition must be logged"
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_grace_expiry_noop_when_not_in_grace_window():
    """An active (or already-restored) Luciel is never deleted by the sweep."""
    from datetime import UTC, datetime

    from app.lifecycle.grace_expiry import expire_grace_window_if_due

    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)  # state = active
    async with tenant_session(admin) as s:
        assert await expire_grace_window_if_due(s, admin_id=admin, now=datetime.now(UTC)) is None
