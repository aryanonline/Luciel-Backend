"""Restore returns each connection's PRE-DELETE status (§3.6.4 — C8, migration 0049).

Restore used to set every revived connection to a blanket 'unconfigured', telling
the owner to reconnect everything even though grace never destroys secrets. Now
delete stashes each row's status/detail and restore puts the truth back — with
the c21 foreign-claim strip keeping precedence, superseded rows staying revoked,
and legacy rows (no stash) keeping the conservative old behavior.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.lifecycle import state_machine
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _seed_instance(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{uuid.uuid4().hex[:10]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


def _conn(admin, iid, ctype, provider, status, detail=None, config=None) -> InstanceConnection:
    return InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=admin,
        instance_id=iid,
        connection_type=ctype,
        auth_class="oauth_token",
        provider=provider,
        status=status,
        status_detail=detail,
        non_secret_config=config or {},
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_returns_pre_delete_statuses():
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        s.add(_conn(admin, iid, "calendar", "google_calendar", "connected"))
        s.add(
            _conn(
                admin,
                iid,
                "crm",
                "hubspot",
                "expired",
                detail="OAuth refresh token expired — reconnect in dashboard",
            )
        )
        await s.flush()
        await state_machine.delete_luciel(s, admin, actor_user_id=admin)
        rows = (
            (
                await s.execute(
                    select(InstanceConnection).where(InstanceConnection.admin_id == admin)
                )
            )
            .scalars()
            .all()
        )
        # Round 7 WP-5: the delete writes QUIESCING (routable, draining), not revoked.
        assert all(r.status == state_machine.QUIESCING for r in rows)
        assert {r.status_stashed for r in rows} == {"connected", "expired"}

        await state_machine.restore(s, admin, actor_user_id=admin)
        by_type = {
            r.connection_type: r
            for r in (
                await s.execute(
                    select(InstanceConnection).where(InstanceConnection.admin_id == admin)
                )
            )
            .scalars()
            .all()
        }
        # "Exactly as it was": the healthy calendar is Connected again; the CRM
        # keeps its honest pre-delete Reconnect-needed truth, detail included.
        assert by_type["calendar"].status == "connected"
        assert by_type["crm"].status == "expired"
        assert "reconnect" in (by_type["crm"].status_detail or "").lower()
        assert all(r.status_stashed is None for r in by_type.values())
        assert all(r.status_detail_stashed is None for r in by_type.values())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_legacy_row_without_stash_keeps_conservative_unconfigured():
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        # A row that entered grace BEFORE 0049: revoked with no stash.
        s.add(_conn(admin, iid, "calendar", "google_calendar", "revoked"))
        inst = (await s.execute(select(Instance).where(Instance.admin_id == admin))).scalar_one()
        inst.state = LucielState.luciel_grace_window
        inst.grace_window_started_at = state_machine._now()
        await s.flush()

        await state_machine.restore(s, admin, actor_user_id=admin)
        row = (
            await s.execute(select(InstanceConnection).where(InstanceConnection.admin_id == admin))
        ).scalar_one()
        assert row.status == "unconfigured"
