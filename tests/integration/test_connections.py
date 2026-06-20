"""Connections layer internals: universal gate, health/refresh worker, fail-safe
deferred queue, secret store, and the config-snapshot immediate-revoke rule
(Architecture §3.8.5 / §3.8.7 rules A, D, E).

All provider/secret seams use deterministic local impls — NO real provider/OAuth/
AWS call (the owner-deferred seam). Time is injected so sweeps need no wall clock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.connections import deferred_queue, gate, service
from app.connections.health import (
    ProbeResult,
    check_connection,
    run_health_sweep,
)
from app.connections.secrets import LocalSecretStore, secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance, LucielState

pytestmark = [pytest.mark.asyncio]


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
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return iid


# --- deterministic provider seams ------------------------------------------ #
class OkProbe:
    async def probe(self, connection: InstanceConnection) -> ProbeResult:
        return ProbeResult(ok=True)


class FailProbe:
    async def probe(self, connection: InstanceConnection) -> ProbeResult:
        return ProbeResult(ok=False, detail="provider 503")


class OkRefresher:
    def __init__(self, new_expiry: datetime) -> None:
        self._exp = new_expiry

    async def refresh(self, connection, store) -> datetime | None:
        return self._exp


class FailRefresher:
    async def refresh(self, connection, store) -> datetime | None:
        return None  # signals refresh failure → expired (§3.8.5 step 5)


class OkDeliverer:
    async def deliver(self, action: DeferredAction) -> bool:
        return True


class FailDeliverer:
    async def deliver(self, action: DeferredAction) -> bool:
        return False


# --- universal gate (§3.8.7 rule A) ----------------------------------------- #
@pytest.mark.usefixtures("db_engine_reset")
async def test_gate_connected_only():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google_calendar",
                status="connected",
            )
        )
    async with tenant_session(admin) as s:
        assert await gate.connection_healthy(
            s, admin_id=admin, instance_id=iid, connection_type="calendar"
        )
        assert (
            await gate.connection_status(
                s, admin_id=admin, instance_id=iid, connection_type="calendar"
            )
            == gate.CONNECTED
        )
        # A type with no connection is not healthy.
        assert not await gate.connection_healthy(
            s, admin_id=admin, instance_id=iid, connection_type="crm"
        )


@pytest.mark.usefixtures("db_engine_reset")
async def test_gate_reads_live_status_immediate_revoke():
    """§3.8.7 rule E (safety side): a mid-conversation revoke takes effect at once —
    the gate reads LIVE status, no cached snapshot."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google_calendar",
                status="connected",
            )
        )
    async with tenant_session(admin) as s:
        assert await gate.connection_healthy(
            s, admin_id=admin, instance_id=iid, connection_type="calendar"
        )
    # Revoke, then immediately re-gate: must be unhealthy now.
    async with tenant_session(admin) as s:
        await service.revoke(s, admin_id=admin, connection_id=cid, user_id=admin)
    async with tenant_session(admin) as s:
        assert not await gate.connection_healthy(
            s, admin_id=admin, instance_id=iid, connection_type="calendar"
        )


# --- health + refresh worker (§3.8.5) --------------------------------------- #
@pytest.mark.usefixtures("db_engine_reset")
async def test_health_probe_ok_keeps_connected():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        c = InstanceConnection(
            connection_id=cid,
            admin_id=admin,
            instance_id=iid,
            connection_type="api",
            auth_class="api_key",
            provider="x",
            status="connected",
        )
        s.add(c)
    async with tenant_session(admin) as s:
        c = await s.get(InstanceConnection, cid)
        status = await check_connection(
            s,
            connection=c,
            probe=OkProbe(),
            refresher=FailRefresher(),
            store=LocalSecretStore(),
            now=now,
        )
        assert status == "connected"
        assert c.last_health_check_at is not None


@pytest.mark.usefixtures("db_engine_reset")
async def test_health_probe_fail_goes_error():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="api",
                auth_class="api_key",
                provider="x",
                status="connected",
            )
        )
    async with tenant_session(admin) as s:
        c = await s.get(InstanceConnection, cid)
        status = await check_connection(
            s,
            connection=c,
            probe=FailProbe(),
            refresher=FailRefresher(),
            store=LocalSecretStore(),
        )
        assert status == "error"
        assert c.status_detail == "provider 503"


@pytest.mark.usefixtures("db_engine_reset")
async def test_refresh_failure_goes_expired():
    """Near-expiry oauth token whose refresh fails → expired (§3.8.5 step 5)."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google_calendar",
                status="connected",
                non_secret_config={"token_expires_at": (now + timedelta(minutes=5)).isoformat()},
            )
        )
    async with tenant_session(admin) as s:
        c = await s.get(InstanceConnection, cid)
        status = await check_connection(
            s,
            connection=c,
            probe=OkProbe(),
            refresher=FailRefresher(),
            store=LocalSecretStore(),
            now=now,
        )
        assert status == "expired"


@pytest.mark.usefixtures("db_engine_reset")
async def test_refresh_success_persists_new_expiry():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = uuid.uuid4()
    now = datetime.now(UTC)
    new_exp = now + timedelta(hours=2)
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google_calendar",
                status="connected",
                non_secret_config={"token_expires_at": (now + timedelta(minutes=5)).isoformat()},
            )
        )
    async with tenant_session(admin) as s:
        c = await s.get(InstanceConnection, cid)
        status = await check_connection(
            s,
            connection=c,
            probe=OkProbe(),
            refresher=OkRefresher(new_exp),
            store=LocalSecretStore(),
            now=now,
        )
        assert status == "connected"
        assert c.non_secret_config["token_expires_at"] == new_exp.isoformat()


@pytest.mark.usefixtures("db_engine_reset")
async def test_health_sweep_one_shot_over_tenant():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        for t in ("calendar", "crm"):
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type=t,
                    auth_class="api_key",
                    provider="x",
                    status="connected",
                )
            )
    async with tenant_session(admin) as s:
        out = await run_health_sweep(
            s,
            admin_id=admin,
            probe=OkProbe(),
            refresher=FailRefresher(),
            store=LocalSecretStore(),
        )
        assert len(out) == 2
        assert all(v == "connected" for v in out.values())


# --- fail-safe deferred queue (§3.8.7 rule D) ------------------------------- #
@pytest.mark.usefixtures("db_engine_reset")
async def test_deferred_delivers_when_connection_healthy():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="sendgrid",
                status="connected",
            )
        )
        await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="send_email",
            connection_type="email_sender",
            payload={"to": "x@y.com"},
        )
    async with tenant_session(admin) as s:
        counts = await deferred_queue.run_sweep(s, admin_id=admin, deliverer=OkDeliverer())
        assert counts["delivered"] == 1


@pytest.mark.usefixtures("db_engine_reset")
async def test_deferred_retries_when_connection_unhealthy():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        # No connection row → unhealthy; do not deliver blind (§3.8.7 rule C).
        await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="send_email",
            connection_type="email_sender",
            payload={},
        )
    async with tenant_session(admin) as s:
        counts = await deferred_queue.run_sweep(s, admin_id=admin, deliverer=OkDeliverer())
        assert counts["retried"] == 1
        assert counts["delivered"] == 0
        action = (await s.execute(_select_one(DeferredAction))).scalars().first()
        assert action.attempts == 1
        assert action.last_error == "connection_unhealthy"


@pytest.mark.usefixtures("db_engine_reset")
async def test_deferred_abandons_past_ttl_with_audit():
    """§3.8.7 rule D: past the 24h max-age TTL → abandoned + owner-notify audit,
    NEVER silently dropped."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    created = datetime.now(UTC) - timedelta(hours=25)
    async with tenant_session(admin) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="send_sms",
            connection_type="sms_sender",
            payload={},
            now=created,
        )
        aid = action.id
    # Sweep "now" — the item is 25h old, past its 24h TTL.
    async with tenant_session(admin) as s:
        counts = await deferred_queue.run_sweep(s, admin_id=admin, deliverer=OkDeliverer())
        assert counts["abandoned"] == 1
        action = await s.get(DeferredAction, aid)
        assert action.status == "abandoned"
        assert action.last_error == "ttl_exceeded"
    # The abandon audit row exists (durable record, §3.8.7 rule D).
    from app.models.audit import AdminAuditLog

    async with tenant_session(admin) as s:
        rows = (await s.execute(_select_one(AdminAuditLog))).scalars().all()
        assert any(r.event_payload.get("action") == "deferred_abandoned" for r in rows)


@pytest.mark.usefixtures("db_engine_reset")
async def test_deferred_delivery_failure_bumps_attempts():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="sendgrid",
                status="connected",
            )
        )
        await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="send_email",
            connection_type="email_sender",
            payload={},
        )
    async with tenant_session(admin) as s:
        counts = await deferred_queue.run_sweep(s, admin_id=admin, deliverer=FailDeliverer())
        assert counts["retried"] == 1
        action = (await s.execute(_select_one(DeferredAction))).scalars().first()
        assert action.last_error == "delivery_failed"


# --- secret store round-trip + idempotent delete (§3.8.3 / §3.6.3) ---------- #
async def test_secret_store_roundtrip_and_idempotent_delete():
    LocalSecretStore.reset()
    store = LocalSecretStore()
    ref = secret_ref_for(uuid.uuid4(), uuid.uuid4(), "calendar")
    await store.put(ref, {"access_token": "tok"})
    assert (await store.get(ref)) == {"access_token": "tok"}
    assert await store.delete(ref) is True
    # Idempotent: deleting again returns False, does not raise (§3.6.3).
    assert await store.delete(ref) is False
    assert (await store.get(ref)) is None


def _select_one(model):
    from sqlalchemy import select

    return select(model)


# ---------------------------------------------------------------------------
# Regression tests for Axis5-F04 (proven-before-cutover reconnect)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reconnect_preserves_connected_status_until_confirm():
    """Axis5-F04: reconnect() must NOT immediately drop the connection to unconfigured.
    The existing connected status is preserved until confirm_reconnect() is called."""
    from app.connections.service import reconnect

    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@rc.test",
                email_lower=f"{admin}@rc.test",
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
        cid = uuid.uuid4()
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google",
                status="connected",
            )
        )
        await s.flush()

    async with tenant_session(admin) as s:
        result = await reconnect(s, admin_id=admin, connection_id=cid, user_id=None)
        assert result is not None
        conn, start_result = result
        # CRITICAL: status must NOT be "unconfigured" — the old connection stays live.
        assert conn.status == "connected", (
            f"reconnect() must preserve connected status; got {conn.status!r}"
        )
        assert conn.status_detail is not None and "reconnect_pending" in conn.status_detail


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_confirm_reconnect_on_probe_success_sets_connected():
    """Axis5-F04: confirm_reconnect() with probe_ok=True sets status=connected."""
    from app.connections.service import confirm_reconnect, reconnect

    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@rc2.test",
                email_lower=f"{admin}@rc2.test",
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
                name="T2",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        cid = uuid.uuid4()
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google",
                status="connected",
            )
        )
        await s.flush()

    async with tenant_session(admin) as s:
        await reconnect(s, admin_id=admin, connection_id=cid, user_id=None)

    async with tenant_session(admin) as s:
        ok, reason = await confirm_reconnect(
            s, admin_id=admin, connection_id=cid, user_id=None, probe_ok=True
        )
        assert ok is True
        assert reason == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_confirm_reconnect_on_probe_failure_restores_prior_status():
    """Axis5-F04: confirm_reconnect() with probe_ok=False restores the prior status."""
    from app.connections.service import confirm_reconnect, reconnect

    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@rc3.test",
                email_lower=f"{admin}@rc3.test",
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
                name="T3",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        cid = uuid.uuid4()
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google",
                status="connected",
            )
        )
        await s.flush()

    async with tenant_session(admin) as s:
        await reconnect(s, admin_id=admin, connection_id=cid, user_id=None)

    async with tenant_session(admin) as s:
        ok, reason = await confirm_reconnect(
            s,
            admin_id=admin,
            connection_id=cid,
            user_id=None,
            probe_ok=False,
            probe_detail="oauth_token_invalid",
        )
        assert ok is False

    # Verify the prior connection status is restored, not left as unconfigured.
    from sqlalchemy import select as sa_select

    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                sa_select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        assert conn.status == "connected", (
            f"Prior status must be restored on failed health check; got {conn.status!r}"
        )
