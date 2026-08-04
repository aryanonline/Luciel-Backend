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
from app.channels.email_routing import MxDelegationResult, NullMxProbe
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


class _ErrorMxProbe:
    """Probe that always returns UNKNOWN (simulates an unreachable/erroring DNS
    resolver) -- mirrors test_number_operability.py's _ErrorProbe."""

    async def check(self, domain: str, expected_mx_host: str) -> MxDelegationResult:
        return MxDelegationResult.UNKNOWN


@pytest.mark.usefixtures("db_engine_reset")
async def test_reverify_with_no_email_provisioned_returns_none():
    """Harmony fix 2026-08-04 item 5: nothing to re-verify if the tenant never
    provisioned an email address at all."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        conn = await service.reverify_email_routing(s, admin_id=admin, instance_id=iid)
        assert conn is None


@pytest.mark.usefixtures("db_engine_reset")
async def test_reverify_is_a_noop_for_vm_subdomain():
    """vm_subdomain has no DNS step and is already connected -- re-verify must return
    it unchanged rather than erroring or re-deriving a status it never needed."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        provisioned = await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="vm_subdomain",
            email_address=None,
            instance_name="Acme",
            user_id=admin,
        )
        conn = await service.reverify_email_routing(
            s,
            admin_id=admin,
            instance_id=iid,
            probe=NullMxProbe(),  # would report NOT_DELEGATED if consulted
        )
        assert conn is not None
        assert conn.connection_id == provisioned.connection_id
        assert conn.status == "connected"
        assert conn.status_detail is None


@pytest.mark.usefixtures("db_engine_reset")
async def test_reverify_own_domain_stays_pending_when_mx_not_yet_delegated():
    """This is the dead-end this item closes: previously pending_email_routing had NO
    way forward at all. Now re-verify runs the probe and, while the record still isn't
    there, correctly stays pending with an actionable detail (never silently flips to
    connected without a confirmed result)."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="hello@acme.com",
            instance_name="Acme",
            user_id=admin,
        )
        conn = await service.reverify_email_routing(
            s,
            admin_id=admin,
            instance_id=iid,
            probe=NullMxProbe(delegated_domains=set()),  # MX record not added yet
        )
        assert conn is not None
        assert conn.status == "pending_email_routing"
        assert conn.status_detail != service.EMAIL_ROUTING_DETAIL  # refreshed, actionable
        assert "mx record" in (conn.status_detail or "").lower()


@pytest.mark.usefixtures("db_engine_reset")
async def test_reverify_own_domain_transitions_to_connected_once_delegated():
    """The success path: admin adds the MX record at their registrar, clicks
    Re-verify -- the probe confirms delegation and the connection finally leaves
    pending_email_routing."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="hello@acme.com",
            instance_name="Acme",
            user_id=admin,
        )
        # First re-verify before the record is added: stays pending (repeatable, no change).
        c1 = await service.reverify_email_routing(
            s, admin_id=admin, instance_id=iid, probe=NullMxProbe(delegated_domains=set())
        )
        assert c1 is not None and c1.status == "pending_email_routing"
        # Admin adds the record -- re-verify again, now confirmed delegated.
        c2 = await service.reverify_email_routing(
            s,
            admin_id=admin,
            instance_id=iid,
            probe=NullMxProbe(delegated_domains={"acme.com"}),
        )
        assert c2 is not None
        assert c2.connection_id == c1.connection_id
        assert c2.status == "connected"
        assert c2.status_detail is not None and "confirmed" in c2.status_detail.lower()
        assert await service.resolve_email_address(s, admin, iid) == "hello@acme.com"


@pytest.mark.usefixtures("db_engine_reset")
async def test_reverify_probe_error_never_defaults_to_connected():
    """A DNS lookup failure (timeout, unreachable resolver) must NEVER silently flip
    the connection to connected -- that would reintroduce the SMS-style
    silent-failure bug in the email flow. It stays pending with an honest, distinct
    'try again' detail."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="hello@acme.com",
            instance_name="Acme",
            user_id=admin,
        )
        conn = await service.reverify_email_routing(
            s, admin_id=admin, instance_id=iid, probe=_ErrorMxProbe()
        )
        assert conn is not None
        assert conn.status == "pending_email_routing"
        assert "couldn't look up" in (conn.status_detail or "").lower()


@pytest.mark.usefixtures("db_engine_reset")
async def test_reverify_reads_current_address_after_reprovision():
    """Re-provisioning to a different own_domain address must have re-verify check the
    NEW domain, never a stale remembered one (mirrors the SMS
    reverify-reads-current-number invariant)."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="hello@old-domain.com",
            instance_name="Acme",
            user_id=admin,
        )
        await service.provision_email(
            s,
            admin_id=admin,
            instance_id=iid,
            mode="own_domain",
            email_address="hello@new-domain.com",
            instance_name="Acme",
            user_id=admin,
        )
        # Only old-domain.com is "delegated" in this probe; new-domain.com is not.
        conn = await service.reverify_email_routing(
            s,
            admin_id=admin,
            instance_id=iid,
            probe=NullMxProbe(delegated_domains={"old-domain.com"}),
        )
        assert conn is not None
        assert conn.status == "pending_email_routing"  # checked new-domain.com, not old
