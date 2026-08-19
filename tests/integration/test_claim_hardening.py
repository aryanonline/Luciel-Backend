"""Destination-claim hardening (c21) — the four adversarially-reviewed gaps.

  (a) a revoked row cannot quietly re-enter service: lifecycle restore is the ONE
      legitimate un-revoke, and it strips a destination another tenant claimed
      during the grace window (and never resurrects a superseded row);
      submit_credentials / reconnect refuse revoked rows outright;
  (b) concurrent claims serialize on a per-destination advisory lock, and the
      0045 partial unique index makes duplicate live sms claims structurally
      impossible;
  (c) squatting: a failed-probe number holds NO claim, so the real owner can
      still connect it; promotion of a pending number re-runs the ownership gate.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import (
    STATUS_PENDING_REGISTRATION,
    NumberAlreadyClaimed,
    attach_sms_number,
    reverify_sms_number,
)
from app.connections import service
from app.db.session import tenant_session
from app.lifecycle import state_machine
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

pytestmark = [pytest.mark.asyncio]

NUM = "+16045550170"


async def _seed(admin: uuid.UUID, *, state: LucielState = LucielState.active) -> uuid.UUID:
    iid = uuid.uuid4()
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
                name="Hard Co",
                state=state,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


async def _attach(admin: uuid.UUID, iid: uuid.UUID, number: str = NUM) -> None:
    async with tenant_session(admin) as s:
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=number,
            probe=NullOperabilityProbe(operable_numbers={number}),
        )


async def _sms_row(admin: uuid.UUID) -> InstanceConnection | None:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin,
                    InstanceConnection.connection_type == "sms_sender",
                )
            )
        ).scalars().first()


# --------------------------------------------------------------------------- #
# (a) revoked-row re-entry
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_strips_a_destination_claimed_by_another_tenant_during_grace():
    """Tenant A deletes (rows revoked → number released), tenant B legitimately
    claims the number, tenant A restores. The restore must NOT capture B's inbound:
    A's row comes back unconfigured with the claim stripped and an honest detail."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a_iid = await _seed(a)
    b_iid = await _seed(b)
    await _attach(a, a_iid)
    async with tenant_session(a) as s:
        await state_machine.delete_luciel(s, a, actor_user_id=a)
    # B claims the number A's deletion released.
    await _attach(b, b_iid)
    # A restores within grace.
    async with tenant_session(a) as s:
        await state_machine.restore(s, a, actor_user_id=a)
    row = await _sms_row(a)
    assert row is not None and row.status == "unconfigured"
    assert (row.non_secret_config or {}).get("destination") is None
    assert "connected to a different account" in (row.status_detail or "")
    # B's claim is untouched.
    b_row = await _sms_row(b)
    assert b_row is not None
    assert (b_row.non_secret_config or {}).get("destination") == NUM


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_keeps_an_unclaimed_destination_intact():
    """Nobody claimed the number during grace → restore brings it back exactly."""
    a = uuid.uuid4()
    a_iid = await _seed(a)
    await _attach(a, a_iid)
    async with tenant_session(a) as s:
        await state_machine.delete_luciel(s, a, actor_user_id=a)
        await state_machine.restore(s, a, actor_user_id=a)
    row = await _sms_row(a)
    assert row is not None and row.status == "unconfigured"
    assert (row.non_secret_config or {}).get("destination") == NUM


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_credentials_and_reconnect_refuse_a_revoked_row():
    """A revoked row is dead: neither a credential submit nor a reconnect can
    resurrect it (each answers not-found instead of quietly re-routing)."""
    a = uuid.uuid4()
    a_iid = await _seed(a)
    await _attach(a, a_iid)
    row = await _sms_row(a)
    assert row is not None
    async with tenant_session(a) as s:
        assert await service.revoke(s, admin_id=a, connection_id=row.connection_id, user_id=None)
    async with tenant_session(a) as s:
        submitted = await service.submit_credentials(
            s,
            admin_id=a,
            connection_id=row.connection_id,
            fields={"accountSid": "AC" + "0" * 32, "authToken": "tok"},
            user_id=None,
        )
        assert submitted is None
        reconnected = await service.reconnect(
            s, admin_id=a, connection_id=row.connection_id, user_id=None
        )
        assert reconnected is None


# --------------------------------------------------------------------------- #
# (b) TOCTOU + the structural index
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_concurrent_claims_for_one_number_admit_exactly_one_tenant():
    """Two tenants race the same number in concurrent transactions: the advisory
    lock serializes them, the loser re-checks and gets the honest refusal."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a_iid = await _seed(a)
    b_iid = await _seed(b)

    async def claim(admin: uuid.UUID, iid: uuid.UUID) -> str:
        try:
            async with tenant_session(admin) as s:
                await attach_sms_number(
                    s,
                    admin_id=admin,
                    instance_id=iid,
                    e164_number=NUM,
                    probe=NullOperabilityProbe(operable_numbers={NUM}),
                )
                return "claimed"
        except (NumberAlreadyClaimed, IntegrityError):
            return "refused"

    outcomes = await asyncio.gather(claim(a, a_iid), claim(b, b_iid))
    assert sorted(outcomes) == ["claimed", "refused"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_unique_index_refuses_a_second_live_claim_structurally():
    """Belt for the gate: even a code path that FORGOT the ownership check cannot
    write a second live sms claim — the 0045 partial unique index refuses."""
    a, b = uuid.uuid4(), uuid.uuid4()
    a_iid = await _seed(a)
    b_iid = await _seed(b)
    await _attach(a, a_iid)
    with pytest.raises(IntegrityError):
        async with tenant_session(b) as s:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=b,
                    instance_id=b_iid,
                    connection_type="sms_sender",
                    auth_class="api_key",
                    provider="twilio",
                    status="connected",
                    non_secret_config={"destination": NUM},
                )
            )
            await s.flush()


# --------------------------------------------------------------------------- #
# (c) squatting
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_probe_number_does_not_block_the_real_owner():
    """Squatter attaches a number their account cannot operate (probe fails) —
    no claim is held, so the REAL owner's operable attach succeeds."""
    squatter, owner = uuid.uuid4(), uuid.uuid4()
    sq_iid = await _seed(squatter)
    ow_iid = await _seed(owner)
    async with tenant_session(squatter) as s:
        await attach_sms_number(
            s,
            admin_id=squatter,
            instance_id=sq_iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers=set()),  # cannot operate it
        )
    async with tenant_session(owner) as s:
        conn = await attach_sms_number(
            s,
            admin_id=owner,
            instance_id=ow_iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers={NUM}),
        )
        assert conn.status == STATUS_PENDING_REGISTRATION
        assert conn.non_secret_config["destination"] == NUM


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_promoting_a_pending_number_rechecks_ownership():
    """The squatter later hosts the number and re-verifies — but the real owner
    claimed it in between. Promotion is a claim point: honest refusal, no write."""
    squatter, owner = uuid.uuid4(), uuid.uuid4()
    sq_iid = await _seed(squatter)
    ow_iid = await _seed(owner)
    async with tenant_session(squatter) as s:
        await attach_sms_number(
            s,
            admin_id=squatter,
            instance_id=sq_iid,
            e164_number=NUM,
            probe=NullOperabilityProbe(operable_numbers=set()),
        )
    await _attach(owner, ow_iid)  # the real owner claims it
    with pytest.raises(NumberAlreadyClaimed):
        async with tenant_session(squatter) as s:
            await reverify_sms_number(
                s,
                admin_id=squatter,
                instance_id=sq_iid,
                probe=NullOperabilityProbe(operable_numbers={NUM}),
            )
