"""The deferred-retry and connection-health sweeps, now that something calls them (P1-7/P1-8).

Two more instances of the defect P0-8 named: correct, tested code with no caller.
``deferred_queue.run_sweep`` and ``health.run_health_sweep`` were both reachable only
from the test suite, so in production a queued callback sat at attempt 0 until the heat
death of the tenant, and a connection's status was whatever it had been the day the
admin created it.

These tests are about the newly-added half — discovery, the entrypoints, and the
production seams they bind — not about the queue and health-check mechanics, which
``test_connections.py`` already covers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.connections import deferred_queue
from app.connections.secrets import LocalSecretStore
from app.db.session import tenant_session
from app.jobs import sweeps
from app.models.connection import InstanceConnection
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


async def _seed_tenant(admin_id: uuid.UUID) -> uuid.UUID:
    instance_id = uuid.uuid4()
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
            )
        )
        s.add(
            Instance(
                id=instance_id,
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return instance_id


async def _seed_connection(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    connection_type: str = "channel_auth",
    auth_class: str = "api_key",
    status: str = "connected",
    secret_ref: str | None = None,
    non_secret_config: dict[str, object] | None = None,
    provider: str = "twilio",
) -> uuid.UUID:
    connection_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            InstanceConnection(
                connection_id=connection_id,
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type=connection_type,
                auth_class=auth_class,
                provider=provider,
                status=status,
                secret_ref=secret_ref,
                non_secret_config=non_secret_config,
            )
        )
    return connection_id


# --- discovery --------------------------------------------------------------- #


async def test_deferred_discovery_finds_only_tenants_with_something_queued() -> None:
    queued, idle = uuid.uuid4(), uuid.uuid4()
    queued_instance = await _seed_tenant(queued)
    await _seed_tenant(idle)
    async with tenant_session(queued) as s:
        await deferred_queue.enqueue(
            s,
            admin_id=queued,
            instance_id=queued_instance,
            action_type="send_email",
            connection_type="email_sender",
            payload={},
        )

    candidates = await sweeps.sweep_candidates(sweeps.DEFERRED)
    assert queued in candidates
    assert idle not in candidates


async def test_deferred_discovery_drops_a_tenant_once_the_queue_is_settled() -> None:
    """Discovery is 'has anything queued', so a delivered/abandoned item stops costing
    a per-tenant transaction on every hourly sweep forever."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    async with tenant_session(admin_id) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type="send_email",
            connection_type="email_sender",
            payload={},
        )
        action.status = "delivered"

    assert admin_id not in await sweeps.sweep_candidates(sweeps.DEFERRED)


async def test_health_discovery_skips_a_tenant_whose_connections_are_all_revoked() -> None:
    live, revoked = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(live, await _seed_tenant(live))
    await _seed_connection(revoked, await _seed_tenant(revoked), status="revoked")

    candidates = await sweeps.sweep_candidates(sweeps.HEALTH)
    assert live in candidates
    assert revoked not in candidates


# --- the deferred sweep ------------------------------------------------------ #


async def test_the_deferred_sweep_abandons_a_past_ttl_item_through_discovery() -> None:
    """The end the rule-D promise actually depends on: with no caller, an undeliverable
    follow-up was neither retried nor abandoned-with-notice — it was silently kept."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    async with tenant_session(admin_id) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type="send_email",
            connection_type="email_sender",
            payload={},
            now=NOW - timedelta(hours=25),
        )
        action_id = action.id

    assert await sweeps.run_deferred_retry(now=NOW) == [str(admin_id)]

    async with tenant_session(admin_id) as s:
        row = await s.get(DeferredAction, action_id)
        assert row.status == "abandoned"
        assert row.last_error == "ttl_exceeded"


async def test_the_deferred_sweep_reports_nothing_when_no_item_is_due_yet() -> None:
    """A tenant is 'acted upon' only if the pass changed something — an hourly sweep
    that lists every tenant with a pending callback tells the operator nothing."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    async with tenant_session(admin_id) as s:
        await deferred_queue.enqueue(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type="send_email",
            connection_type="email_sender",
            payload={},
            now=NOW,
        )

    assert await sweeps.run_deferred_retry(now=NOW - timedelta(minutes=5)) == []


async def test_a_callback_is_not_attempted_before_the_time_the_lead_asked_for() -> None:
    """Booking time and promised time are different clocks. While nothing swept the
    queue the distinction was invisible; an hourly worker would otherwise fire every
    callback the moment it was booked, and expire the far-future ones against a TTL
    measured from booking."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    fire_at = NOW + timedelta(days=3)
    async with tenant_session(admin_id) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type="schedule_callback",
            connection_type="sms_sender",
            payload={"fire_at": fire_at.isoformat(), "channel": "sms", "message": "hi"},
            now=NOW,
        )
        assert action.next_attempt_at == fire_at
        # The 24h retry window runs from the promise, not the booking — otherwise a
        # callback three days out is abandoned two days before it is due.
        assert action.expires_at == fire_at + timedelta(hours=24)

    assert await sweeps.run_deferred_retry(now=NOW + timedelta(days=1)) == []
    async with tenant_session(admin_id) as s:
        assert (await s.get(DeferredAction, action.id)).status == "queued"


async def test_an_unparseable_fire_at_is_delivered_rather_than_held_forever() -> None:
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    async with tenant_session(admin_id) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type="schedule_callback",
            connection_type="sms_sender",
            payload={"fire_at": "next tuesday-ish", "channel": "sms", "message": "hi"},
            now=NOW,
        )
    assert action.next_attempt_at == NOW


async def test_the_production_deliverer_reports_an_unsendable_channel_as_undelivered() -> None:
    """``send_outbound_reply`` answers with data, not exceptions, so a Deliverer that
    treated 'we tried' as success would mark the row delivered and drop the lead's
    callback — the exact silent loss rule D exists to prevent."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    lead_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin_id,
                instance_id=instance_id,
                contact_identifier="+15550000001",
                last_activity_at=NOW,
            )
        )
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type="schedule_callback",
            connection_type="sms_sender",
            payload={"fire_at": NOW.isoformat(), "channel": "sms", "message": "calling back"},
            resolved_lead_id=lead_id,
            now=NOW,
        )
        # No provisioned SMS sender for this tenant, so nothing can leave the process.
        assert await deferred_queue.ChannelDeliverer(s).deliver(action) is False


async def test_the_production_deliverer_refuses_a_callback_with_no_reachable_lead() -> None:
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    async with tenant_session(admin_id) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type="schedule_callback",
            connection_type="sms_sender",
            payload={"fire_at": NOW.isoformat(), "channel": "sms", "message": "calling back"},
            now=NOW,
        )
        assert await deferred_queue.ChannelDeliverer(s).deliver(action) is False


# --- the health sweep -------------------------------------------------------- #


async def test_the_health_sweep_marks_a_connection_whose_credential_vanished() -> None:
    """The failure the probe exists to catch: a secret deleted out from under a
    connection that still says ``connected``, so every use of it fails one customer at
    a time instead of once, visibly, in the dashboard."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    connection_id = await _seed_connection(admin_id, instance_id, secret_ref="vm/gone")

    assert await sweeps.run_connection_health(now=NOW) == [str(admin_id)]

    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == connection_id)
            )
        ).scalar_one()
        assert row.status == "error"
        assert row.status_detail == "stored credential is missing — reconnect"
        assert row.last_health_check_at is not None


async def test_a_connection_whose_credential_is_still_there_stays_connected() -> None:
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    connection_id = await _seed_connection(admin_id, instance_id, secret_ref="vm/present")
    store = LocalSecretStore()
    await store.put("vm/present", {"api_key": "k"})

    async with tenant_session(admin_id) as s:
        connection = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == connection_id)
            )
        ).scalar_one()
        from app.connections import health

        assert await health.StoredCredentialProbe(store).probe(connection) == health.ProbeResult(
            ok=True
        )


async def test_a_platform_provisioned_connection_has_no_credential_to_lose() -> None:
    """``provisioned_resource`` connections carry no tenant secret_ref; failing them for
    the absence of one would take out every platform-provisioned channel at once."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    connection_id = await _seed_connection(
        admin_id, instance_id, auth_class="provisioned_resource", secret_ref=None
    )

    await sweeps.run_connection_health(now=NOW)

    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == connection_id)
            )
        ).scalar_one()
        assert row.status == "connected"


async def test_an_expiring_token_is_reported_as_needing_reconnection() -> None:
    """There is no bound token endpoint, so the sweep cannot rotate an OAuth token. It
    says so 30 minutes early rather than letting the token die mid-conversation."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    connection_id = await _seed_connection(
        admin_id,
        instance_id,
        auth_class="oauth_token",
        secret_ref="vm/token",
        non_secret_config={"token_expires_at": (NOW + timedelta(minutes=5)).isoformat()},
    )

    await sweeps.run_connection_health(now=NOW)

    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == connection_id)
            )
        ).scalar_one()
        assert row.status == "expired"
        assert row.status_detail == "refresh failed — reconnect in dashboard"


async def test_the_health_sweep_leaves_a_revoked_connection_alone() -> None:
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    live = await _seed_connection(admin_id, instance_id, secret_ref=None)
    dead = await _seed_connection(
        admin_id, instance_id, connection_type="calendar", status="revoked", secret_ref="vm/gone"
    )

    await sweeps.run_connection_health(now=NOW)

    async with tenant_session(admin_id) as s:
        rows = {
            r.connection_id: r
            for r in (await s.execute(select(InstanceConnection))).scalars().all()
        }
        assert rows[dead].status == "revoked"
        assert rows[dead].last_health_check_at is None
        assert rows[live].status == "connected"


async def test_an_expiring_token_with_a_real_refresher_stays_connected() -> None:
    """§3.8.5 steps 3–4, now for EVERY specced provider: a Google row near expiry
    is refreshed in place — new material persisted, new hint stamped, status
    stays connected. Before app/connections/refresh.py this path existed only
    for Outlook; Google connections just expired."""
    from datetime import UTC, datetime

    from app.connections import health
    from app.connections.secrets import LocalSecretStore

    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    connection_id = await _seed_connection(
        admin_id,
        instance_id,
        auth_class="oauth_token",
        secret_ref="vm/refresh-me",
        non_secret_config={"token_expires_at": (NOW + timedelta(minutes=5)).isoformat()},
    )
    store = LocalSecretStore()
    await store.put("vm/refresh-me", {"access_token": "old", "refresh_token": "rt"})

    class _AlwaysFresh:
        async def refresh(self, connection, backing):  # noqa: ANN001
            await backing.put(connection.secret_ref, {"access_token": "new", "refresh_token": "rt"})
            return datetime.now(UTC) + timedelta(hours=1)

    async with tenant_session(admin_id) as s:
        await health.run_health_sweep(
            session=s,
            admin_id=admin_id,
            probe=health.StoredCredentialProbe(store),
            refresher=_AlwaysFresh(),  # the worker seam takes any TokenRefresher
            store=store,
            now=NOW,
        )

    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == connection_id)
            )
        ).scalar_one()
        assert row.status == "connected"
        new_hint = row.non_secret_config["token_expires_at"]
        assert datetime.fromisoformat(new_hint) > NOW + timedelta(minutes=30)
    assert (await store.get("vm/refresh-me"))["access_token"] == "new"


async def test_ensure_fresh_access_token_refreshes_at_call_time(monkeypatch) -> None:
    """The call-time twin of the sweep (§3.8.5, the tenant_outlook pattern
    generalized): a booking call finding a stale hint refreshes FIRST, so the
    customer's "book me in" never fails on a token that merely aged out between
    sweeps. A fresh hint short-circuits with no provider call."""
    from datetime import UTC, datetime

    from app.connections import refresh
    from app.connections.secrets import LocalSecretStore

    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    await _seed_connection(
        admin_id,
        instance_id,
        connection_type="calendar",
        auth_class="oauth_token",
        secret_ref="vm/cal-token",
        provider="google_calendar",
        non_secret_config={"token_expires_at": (NOW - timedelta(minutes=1)).isoformat()},
    )
    store = LocalSecretStore()
    await store.put("vm/cal-token", {"access_token": "stale", "refresh_token": "rt"})

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"access_token": "fresh", "expires_in": 3600}

    class _Transport:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url, *, data=None, headers=None):  # noqa: ANN001
            self.calls += 1
            return _Response()

        async def get(self, url, *, params=None):  # noqa: ANN001
            raise AssertionError("google refresh is a POST")

    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_GOOGLE_CLIENT_ID", "g-cid")
    monkeypatch.setenv("LUCIEL_GOOGLE_CLIENT_SECRET", "g-csecret")
    monkeypatch.setenv("LUCIEL_GOOGLE_REDIRECT_URI", "https://api.example.com/cb")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(refresh, "live_mode", lambda: True)
    transport = _Transport()

    async with tenant_session(admin_id) as s:
        token = await refresh.ensure_fresh_access_token(
            s, admin_id, instance_id, connection_type="calendar", store=store, transport=transport
        )
        assert token == "fresh"
        assert transport.calls == 1
        # Second call inside the fresh window: no provider call, same token.
        token2 = await refresh.ensure_fresh_access_token(
            s, admin_id, instance_id, connection_type="calendar", store=store, transport=transport
        )
        assert token2 == "fresh"
        assert transport.calls == 1

    assert (await store.get("vm/cal-token"))["access_token"] == "fresh"
    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_type == "calendar",
                )
            )
        ).scalar_one()
        assert datetime.fromisoformat(row.non_secret_config["token_expires_at"]) > datetime.now(
            UTC
        ) + timedelta(minutes=30)
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_the_health_sweep_never_resurrects_a_billing_dormant_connection() -> None:
    """``dormant`` is a BILLING suspension (Legal §A3, app/billing/dunning.py), not a
    health state. A dormant row whose credential is perfectly healthy must stay
    dormant through the sweep — flipping it back to ``connected`` would silently
    re-arm a non-paying account's action tools within one 6-hour sweep cycle. Only
    the payment webhooks may leave dormant."""
    admin_id = uuid.uuid4()
    instance_id = await _seed_tenant(admin_id)
    # No secret_ref: the probe would report this row healthy if it were swept.
    suspended = await _seed_connection(
        admin_id, instance_id, connection_type="calendar", status="dormant", secret_ref=None
    )

    await sweeps.run_connection_health(now=NOW)

    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == suspended)
            )
        ).scalar_one()
        assert row.status == "dormant"
        assert row.last_health_check_at is None
