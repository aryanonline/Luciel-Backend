"""The published 1-year audit retention, enforced rather than asserted (P1-15, §B5).

Every other data class had an enforcer. The audit log was append-only with no ceiling,
which made the published retention a commitment a tenant could disprove by waiting.

These tests care about the SHAPE of the fix as much as the outcome, because the fix
punches a hole in an immutability guarantee that the rest of the system leans on. The
hole has to be exactly one row wide: aged rows only, through one procedure, owned by a
role the app is not a member of. So there are as many tests here for what still cannot
be deleted as for what now can.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit import log as audit
from app.db.session import tenant_session
from app.jobs import sweeps
from app.lifecycle import audit_retention
from app.models.audit import ActorType, AdminAuditLog
from app.models.instance import Account, AccountState
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.asyncio]

AGED = timedelta(days=400)
RECENT = timedelta(days=30)


async def _seed_account(admin_id: uuid.UUID) -> None:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
                # The account must PREDATE its aged audit rows: an audit row older than
                # a year can only exist on an account older than a year (append-only,
                # server-stamped created_at). Discovery relies on exactly this
                # invariant (migration 0034's grant-free age predicate), so seeding a
                # young account with backdated rows would be a state production can
                # never produce.
                created_at=datetime.now(UTC) - AGED - timedelta(days=5),
            )
        )


async def _append(admin_id: uuid.UUID, *, age: timedelta, prev_hash: str | None) -> str:
    """Append one correctly-chained row at a chosen age, and return its hash.

    The rows have to be genuinely old rather than swept with a doctored clock: the
    cutoff is derived server-side from ``now()`` inside the procedure precisely so no
    caller can move it, and the trigger re-derives it a second time.
    """
    created_at = datetime.now(UTC) - age
    event_id = uuid.uuid4()
    payload: dict[str, object] = {"aged": True}
    async with tenant_session(admin_id) as s:
        s.add(
            AdminAuditLog(
                event_id=event_id,
                admin_id=admin_id,
                actor_type=ActorType.platform,
                event_type="login_succeeded",
                event_payload=payload,
                created_at=created_at,
                prev_hash=prev_hash,
            )
        )
    return audit.compute_hash(event_id, created_at, payload)


async def _rows(admin_id: uuid.UUID) -> list[AdminAuditLog]:
    async with tenant_session(admin_id) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin_id)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )


async def test_rows_past_the_year_go_and_everything_younger_stays() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    h1 = await _append(admin_id, age=AGED, prev_hash=None)
    await _append(admin_id, age=AGED - timedelta(days=1), prev_hash=h1)
    async with tenant_session(admin_id) as s:
        await audit.emit(
            s, admin_id=admin_id, actor_type=ActorType.platform, event_type="login_succeeded"
        )

    assert str(admin_id) in await sweeps.run_audit_retention()

    kept = await _rows(admin_id)
    assert [r.event_type for r in kept] == ["login_succeeded", "data_retention_hard_delete"]
    assert all(r.created_at > datetime.now(UTC) - timedelta(days=365) for r in kept)


async def test_the_prune_is_itself_audited_but_only_when_it_removed_something() -> None:
    """A zero-count event per tenant per run would make the retention job the largest
    writer to the table it exists to bound."""
    quiet, pruned = uuid.uuid4(), uuid.uuid4()
    await _seed_account(quiet)
    await _seed_account(pruned)
    async with tenant_session(quiet) as s:
        await audit.emit(
            s, admin_id=quiet, actor_type=ActorType.platform, event_type="login_succeeded"
        )
    await _append(pruned, age=AGED, prev_hash=None)

    acted = await sweeps.run_audit_retention()
    assert str(pruned) in acted
    assert str(quiet) not in acted

    assert [r.event_type for r in await _rows(quiet)] == ["login_succeeded"]
    events = [r for r in await _rows(pruned) if r.event_type == "data_retention_hard_delete"]
    assert len(events) == 1
    assert events[0].event_payload["data_class"] == "audit_log"
    assert events[0].event_payload["count"] == 1


async def test_the_app_role_still_cannot_delete_an_audit_row_itself() -> None:
    """The exemption is on the audit owner's identity, not on the row's age — so the
    app role gains nothing from the fact that aged rows are now deletable at all."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    await _append(admin_id, age=AGED, prev_hash=None)

    with pytest.raises(DBAPIError):
        async with tenant_session(admin_id) as s:
            await s.execute(
                text("DELETE FROM admin_audit_log WHERE admin_id = :a"), {"a": str(admin_id)}
            )

    assert len(await _rows(admin_id)) == 1


async def test_the_published_year_cannot_be_argued_down_by_the_caller() -> None:
    """``p_retention_days`` is a floor, not a setting: a sweep talked into a 1-day
    retention would erase yesterday's evidence of whoever talked it into that."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    await _append(admin_id, age=RECENT, prev_hash=None)

    async with tenant_session(admin_id) as s:
        deleted = (
            await s.execute(text("SELECT audit_retention_prune(:a, 1)"), {"a": str(admin_id)})
        ).scalar_one()
    assert deleted == 0
    assert len(await _rows(admin_id)) == 1


async def test_the_chain_still_verifies_once_its_aged_prefix_is_gone() -> None:
    """Pruning necessarily orphans the oldest surviving row's ``prev_hash``. That is an
    authorized truncation, not tampering — but a row vanishing from the middle of the
    surviving span still has to be caught."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id)
    h1 = await _append(admin_id, age=AGED, prev_hash=None)
    h2 = await _append(admin_id, age=AGED - timedelta(days=1), prev_hash=h1)
    await _append(admin_id, age=RECENT, prev_hash=h2)
    async with tenant_session(admin_id) as s:
        await audit.emit(
            s, admin_id=admin_id, actor_type=ActorType.platform, event_type="login_succeeded"
        )
        assert await audit.verify_chain(s, admin_id) is True

    async with tenant_session(admin_id) as s:
        assert await audit_retention.prune_audit_log_if_due(s, admin_id=admin_id) == 2
    async with tenant_session(admin_id) as s:
        assert await audit.verify_chain(s, admin_id) is True

    # A hole inside what survived is still a break: append a row that chains from
    # nothing the previous row produced and the replay must reject it.
    await _append(admin_id, age=timedelta(0), prev_hash="0" * 64)
    async with tenant_session(admin_id) as s:
        assert await audit.verify_chain(s, admin_id) is False
