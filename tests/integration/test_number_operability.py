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

import pytest
from app.channels.number_operability import (
    NullOperabilityProbe,
    OperabilityResult,
)
from app.channels.phone_lifecycle import (
    STATUS_NOT_OPERABLE,
    STATUS_PENDING_REGISTRATION,
    STATUS_UNCONFIGURED,
    attach_sms_number,
    reverify_sms_number,
    sms_number_status,
)
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState

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
        assert b.non_secret_config["destination"] == NUM_B  # number changed
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
