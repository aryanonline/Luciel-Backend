"""Connection swap staging (Architecture §3.8.7 rule B, Decision §45).

Unit-level (service-layer, no HTTP) proof that ``swap()`` stages the candidate
provider ALONGSIDE the live one — never mutating ``connection.provider`` in place —
and that ``confirm_reconnect()`` is the only place that ever writes it, on a
confirmed-healthy probe. On a failed probe the old provider was never touched, so
there is nothing to roll back (the bug this closes: previously `swap()` wrote
`connection.provider` immediately and a failed probe left the wrong provider live
even though `status` was correctly restored).
"""

from __future__ import annotations

import uuid

import pytest
from app.connections import service
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState


async def _seed_tenant(admin: uuid.UUID) -> uuid.UUID:
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
                name="Acme",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return iid


@pytest.mark.usefixtures("db_engine_reset")
async def test_swap_does_not_mutate_provider_before_confirm():
    """swap() must NOT write connection.provider — only stage it in non_secret_config."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        conn, _ = await service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="crm",
            provider="hubspot",
            user_id=None,
        )
        await service.confirm_reconnect(
            s, admin_id=admin, connection_id=conn.connection_id, user_id=None, probe_ok=True
        )
        assert conn.provider == "hubspot"

        swapped, _ = await service.swap(
            s,
            admin_id=admin,
            connection_id=conn.connection_id,
            provider="salesforce",
            user_id=None,
        )
        # Alongside-write: the live provider is UNCHANGED until confirm.
        assert swapped.provider == "hubspot"
        assert swapped.status == "connected"  # old stays live (rule B)
        assert (swapped.non_secret_config or {}).get("pending_provider") == "salesforce"


@pytest.mark.usefixtures("db_engine_reset")
async def test_swap_failed_probe_reverts_provider_to_old_value():
    """On a failed health probe, the provider must be the OLD provider — not the
    candidate one — proving there is no stuck in-place mutation to roll back."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        conn, _ = await service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="crm",
            provider="hubspot",
            user_id=None,
        )
        await service.confirm_reconnect(
            s, admin_id=admin, connection_id=conn.connection_id, user_id=None, probe_ok=True
        )
        await service.swap(
            s,
            admin_id=admin,
            connection_id=conn.connection_id,
            provider="salesforce",
            user_id=None,
        )
        ok, reason = await service.confirm_reconnect(
            s,
            admin_id=admin,
            connection_id=conn.connection_id,
            user_id=None,
            probe_ok=False,
            probe_detail="bad_credentials",
        )
        assert not ok and reason == "bad_credentials"
        assert conn.provider == "hubspot"  # never touched — old stays live
        assert conn.status == "connected"
        assert "pending_provider" not in (conn.non_secret_config or {})


@pytest.mark.usefixtures("db_engine_reset")
async def test_swap_confirmed_probe_cuts_over_provider():
    """On a confirmed-healthy probe, the STAGED provider becomes authoritative and
    the staging key is cleared."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        conn, _ = await service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="crm",
            provider="hubspot",
            user_id=None,
        )
        await service.confirm_reconnect(
            s, admin_id=admin, connection_id=conn.connection_id, user_id=None, probe_ok=True
        )
        await service.swap(
            s,
            admin_id=admin,
            connection_id=conn.connection_id,
            provider="salesforce",
            user_id=None,
        )
        ok, state = await service.confirm_reconnect(
            s, admin_id=admin, connection_id=conn.connection_id, user_id=None, probe_ok=True
        )
        assert ok and state == "connected"
        assert conn.provider == "salesforce"
        assert conn.status == "connected"
        assert "pending_provider" not in (conn.non_secret_config or {})
