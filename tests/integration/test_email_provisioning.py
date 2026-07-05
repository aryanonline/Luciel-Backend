"""Email provisioning service (§3.1.6a / Decision #49).

own_domain — stores the admin-supplied address; status is pending_email_routing
  until inbound DNS/MX routing is verified (a non-silent precondition, mirroring
  SMS carrier registration). NOT live yet.
vm_subdomain — derives a zero-DNS address; immediately connected and usable.

The address is non-secret config (§3.8.3): no secret material is written on this path.
"""

from __future__ import annotations

import uuid

import pytest
from app.connections import service
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState

pytestmark = [pytest.mark.asyncio]


async def _seed_tenant(admin: uuid.UUID, name: str = "Acme") -> uuid.UUID:
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
                name=name,
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return iid


@pytest.mark.usefixtures("db_engine_reset")
async def test_own_domain_is_pending_until_routing_verified():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        conn = await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="hello@acme.com",
            instance_name="Acme",
            user_id=admin,
        )
        assert conn.status == "pending_email_routing"
        assert conn.status_detail == service.EMAIL_ROUTING_DETAIL
        assert conn.non_secret_config == {"address": "hello@acme.com", "mode": "own_domain"}
        assert conn.connection_type == "email_sender"
        # Address is the authoritative resolver value; no secret written on this path.
        assert await service.resolve_email_address(s, admin, iid) == "hello@acme.com"


@pytest.mark.usefixtures("db_engine_reset")
async def test_own_domain_requires_an_address():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        with pytest.raises(ValueError):
            await service.provision_email(
                s,
                admin_id=admin,
                instance_id=iid,
                mode="own_domain",
                email_address="",
                instance_name="Acme",
                user_id=admin,
            )


@pytest.mark.usefixtures("db_engine_reset")
async def test_vm_subdomain_is_immediately_connected():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, name="Acme Corp")
    async with tenant_session(admin) as s:
        conn = await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="vm_subdomain",
            email_address=None,
            instance_name="Acme Corp",
            user_id=admin,
        )
        assert conn.status == "connected"
        assert conn.status_detail is None
        addr = conn.non_secret_config["address"]
        # A full, valid email address on the VM subdomain zone (usable as a sender).
        assert addr.startswith("luciel@acme-corp.")
        assert "@" in addr
        assert await service.resolve_email_address(s, admin, iid) == addr


@pytest.mark.usefixtures("db_engine_reset")
async def test_reprovision_reuses_single_active_connection():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        first = await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="vm_subdomain",
            email_address=None,
            instance_name="Acme",
            user_id=admin,
        )
        second = await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="hi@acme.com",
            instance_name="Acme",
            user_id=admin,
        )
        # Single-active per type (§3.8.2): same row, switched to own_domain/pending.
        assert second.connection_id == first.connection_id
        assert second.status == "pending_email_routing"
        current = await service.get_email_provisioning(s, admin, iid)
        assert current is not None and current.connection_id == first.connection_id


@pytest.mark.usefixtures("db_engine_reset")
async def test_dns_records_derived_from_own_domain_address():
    records = service.dns_records_for("hello@acme.com")
    kinds = {r["type"] for r in records}
    assert kinds == {"MX", "TXT", "CNAME"}
    mx = next(r for r in records if r["type"] == "MX")
    assert mx["host"] == "acme.com" and mx["priority"] == 10
