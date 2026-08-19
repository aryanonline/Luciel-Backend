"""SMS number operability gate + honest re-verify (Architecture §3.1.4).

Covers the honesty invariants agreed for the BYO-number flow:
  - operable number → pending_carrier_registration (never "connected" here);
  - NOT-operable number → not_operable_hosting_required (never a "being activated" lie);
  - probe UNKNOWN (provider error) → stays non-live (never defaults to operable);
  - re-verify is stateless + repeatable and transitions ONLY on a confirmed operable
    result, reading the CURRENTLY attached number;
  - number-change invalidation: swapping in a different number re-runs the gate from
    scratch, so a new number can never inherit the prior number's status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.channels.number_operability import (
    NullOperabilityProbe,
    OperabilityResult,
)
from app.channels.phone_lifecycle import (
    STATUS_NOT_OPERABLE,
    STATUS_PENDING_REGISTRATION,
    STATUS_UNCONFIGURED,
    NumberAlreadyClaimed,
    attach_sms_number,
    reverify_sms_number,
    sms_number_status,
)
from app.db.session import tenant_session, untenanted_session
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import text

pytestmark = [pytest.mark.asyncio]

NUM_A = "+14155550111"
NUM_B = "+14155550222"


class _ErrorProbe:
    """Probe that always returns UNKNOWN (simulates a Twilio error/unreachable)."""

    async def check(self, e164_number: str) -> OperabilityResult:
        return OperabilityResult.UNKNOWN


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Op Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_operable_number_goes_pending_registration():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM_A,
            probe=NullOperabilityProbe(operable_numbers={NUM_A}),
        )
        assert conn.status == STATUS_PENDING_REGISTRATION
        # Honest detail: names carrier registration as the TENANT's responsibility,
        # not a platform "being activated" claim.
        assert "responsibility" in (conn.status_detail or "").lower()
        assert "being activated" not in (conn.status_detail or "").lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_non_operable_number_is_hosting_required_not_live():
    """A valid E.164 that is NOT in the Twilio account must NOT show a live/pending
    state — it is rejected into not_operable_hosting_required with hosting guidance."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM_A,
            probe=NullOperabilityProbe(operable_numbers=set()),  # not operable
        )
        assert conn.status == STATUS_NOT_OPERABLE
        assert "host" in (conn.status_detail or "").lower()
        # Channel status surface reflects the non-live state.
        assert await sms_number_status(s, admin_id=admin, instance_id=iid) == STATUS_NOT_OPERABLE


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_probe_error_stays_non_live():
    """A probe UNKNOWN (provider/network error) must NEVER default to operable."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        conn = await attach_sms_number(
            s, admin_id=admin, instance_id=iid, e164_number=NUM_A, probe=_ErrorProbe()
        )
        assert conn.status == STATUS_NOT_OPERABLE


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reverify_transitions_only_on_confirmed_operable():
    """Attach when not-operable → re-verify still not-operable → re-verify once the
    number is operable → transitions. Stateless + repeatable."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        # 1) attach while NOT operable
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM_A,
            probe=NullOperabilityProbe(operable_numbers=set()),
        )
        # 2) re-verify, still not operable → stays non-live (repeatable, no change)
        c = await reverify_sms_number(
            s, admin_id=admin, instance_id=iid, probe=NullOperabilityProbe(operable_numbers=set())
        )
        assert c is not None and c.status == STATUS_NOT_OPERABLE
        c = await reverify_sms_number(
            s, admin_id=admin, instance_id=iid, probe=NullOperabilityProbe(operable_numbers=set())
        )
        assert c is not None and c.status == STATUS_NOT_OPERABLE  # idempotent
        # 3) tenant hosts the number → re-verify now confirms operable → transitions
        c = await reverify_sms_number(
            s, admin_id=admin, instance_id=iid, probe=NullOperabilityProbe(operable_numbers={NUM_A})
        )
        assert c is not None and c.status == STATUS_PENDING_REGISTRATION
        # c21: the confirmed-operable re-verify PROMOTED the pending number to the
        # routable claim.
        assert c.non_secret_config["destination"] == NUM_A
        assert "pending_destination" not in c.non_secret_config


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reverify_reads_current_number_after_swap():
    """Number-change invalidation + re-verify-reads-current: attach operable A (pending),
    then swap to a NON-operable B → status must reset to not-operable (B is not inheriting
    A's pending). Re-verify then checks B (the current number), not A."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        a = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM_A,
            probe=NullOperabilityProbe(operable_numbers={NUM_A}),
        )
        assert a.status == STATUS_PENDING_REGISTRATION
        # Swap to B, which is NOT operable → must NOT inherit A's pending status.
        b = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=NUM_B,
            probe=NullOperabilityProbe(operable_numbers={NUM_A}),  # only A operable, not B
        )
        assert b.connection_id == a.connection_id  # same row (retained)
        # c21 anti-squatting: a NOT-operable number is never the routable claim —
        # it waits under pending_destination, and the old claim on A is released.
        assert b.non_secret_config.get("destination") is None
        assert b.non_secret_config["pending_destination"] == NUM_B
        assert b.status == STATUS_NOT_OPERABLE  # re-derived from scratch for B
        # Re-verify reads the CURRENT number (B); with only A operable, B stays non-live.
        c = await reverify_sms_number(
            s, admin_id=admin, instance_id=iid, probe=NullOperabilityProbe(operable_numbers={NUM_A})
        )
        assert c is not None and c.status == STATUS_NOT_OPERABLE


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reverify_with_no_number_returns_none():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        assert await sms_number_status(s, admin_id=admin, instance_id=iid) == STATUS_UNCONFIGURED
        c = await reverify_sms_number(s, admin_id=admin, instance_id=iid)
        assert c is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_number_held_by_another_tenant_cannot_be_claimed():
    """Cross-tenant uniqueness (§3.1.3): the destination is what inbound SMS AND
    voice route on, so a second tenant claiming an already-held number would
    capture the holder's inbound conversations. Live-caught 2026-08-17: this was
    the one destination-bind path (email and Meta both gate) without the check."""
    holder, claimant = uuid.uuid4(), uuid.uuid4()
    holder_iid = await _seed(holder)
    claimant_iid = await _seed(claimant)
    async with tenant_session(holder) as s:
        await attach_sms_number(
            s,
            admin_id=holder,
            instance_id=holder_iid,
            e164_number=NUM_A,
            probe=NullOperabilityProbe(operable_numbers={NUM_A}),
        )
    async with tenant_session(claimant) as s:
        with pytest.raises(NumberAlreadyClaimed):
            await attach_sms_number(
                s,
                admin_id=claimant,
                instance_id=claimant_iid,
                e164_number=NUM_A,
                probe=NullOperabilityProbe(operable_numbers={NUM_A}),
            )
        # The refusal left nothing behind: the claimant still has no sms_sender row.
        assert (
            await sms_number_status(s, admin_id=claimant, instance_id=claimant_iid)
            == STATUS_UNCONFIGURED
        )
    # The holder's own re-attach of the SAME number is NOT a foreign claim.
    async with tenant_session(holder) as s:
        again = await attach_sms_number(
            s,
            admin_id=holder,
            instance_id=holder_iid,
            e164_number=NUM_A,
            probe=NullOperabilityProbe(operable_numbers={NUM_A}),
        )
        assert again.status == STATUS_PENDING_REGISTRATION


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_legacy_duplicate_claims_resolve_to_the_newest():
    """Rows claimed BEFORE the uniqueness gate existed can still collide. For those
    legacy duplicates ``channel_resolve_destination`` must be deterministic — the
    newest claim wins — not an arbitrary ``LIMIT 1`` pick, which live-routed a
    tenant's voice calls to a stale claimant on 2026-08-17. Pinned on email_sender
    rows since 0045: the partial unique index makes sms_sender duplicates
    structurally impossible, while email_sender scalar duplicates remain possible
    by design (a disconnected platform address keeps its claim)."""
    old_admin, new_admin = uuid.uuid4(), uuid.uuid4()
    old_iid = await _seed(old_admin)
    new_iid = await _seed(new_admin)
    # Simulate the pre-gate state with direct row writes: explicit created_at values,
    # because rows flushed in one transaction share now() and would tie.
    async with tenant_session(old_admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=old_admin,
                instance_id=old_iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="ses",
                status="connected",
                non_secret_config={"destination": "dup@reply.vantagemind.ai"},
                created_at=datetime(2026, 7, 28, tzinfo=UTC),
            )
        )
        await s.flush()
    async with tenant_session(new_admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=new_admin,
                instance_id=new_iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="ses",
                status="connected",
                non_secret_config={"destination": "dup@reply.vantagemind.ai"},
                created_at=datetime(2026, 8, 17, tzinfo=UTC),
            )
        )
        await s.flush()
    async with untenanted_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT admin_id, instance_id"
                    " FROM channel_resolve_destination('email_sender', :d)"
                ),
                {"d": "dup@reply.vantagemind.ai"},
            )
        ).one()
    assert row.admin_id == new_admin
    assert row.instance_id == new_iid
