"""Meta sends go out on the TENANT's Graph grant, never a platform token (P1-1).

``build_meta_sender()`` read ``settings.meta_access_token`` and, in live mode, handed that
one token to every tenant on the process. Two things follow from that, and both are worse
than a missing feature:

  * a tenant who never connected Meta would still get a *live* sender, so the send left
    the process and was rejected by Graph as unauthorized for that page — reported to the
    lead as delivered, because a rejection at Graph is not a rejection here;
  * a tenant who DID connect would send with the platform's grant rather than their own,
    which is the §3.1.4 billing-path problem the BYO-Twilio work already settled for SMS.

The fix mirrors ``tenant_twilio``: the token comes off the same ``channel_auth`` connection
the sender identity comes off, and there is no fallback. These tests pin the four states
that matter — no connection, connection with no token, revoked connection, and the happy
path — plus the absence of the platform setting itself, since re-adding it is how the hole
would reopen.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.meta_sender import GraphMetaSender, build_tenant_meta_sender
from app.channels.whatsapp_adapter import NullMetaSender
from app.config import get_settings
from app.connections.secrets import LocalSecretStore, secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection

from tests.unit.live_settings import LIVE_PLATFORM_SECRET


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch):
    """Live mode via the environment: the builder reads the mode through ``get_settings``."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", LIVE_PLATFORM_SECRET)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def _seed(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    status: str = "connected",
    material: dict[str, str] | None = None,
) -> LocalSecretStore:
    ref = secret_ref_for(admin_id, instance_id, "channel_auth")
    store = LocalSecretStore()
    if material is not None:
        await store.put(ref, material)
    async with tenant_session(admin_id) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type="channel_auth",
                auth_class="oauth",
                provider="meta",
                status=status,
                secret_ref=ref,
                non_secret_config={"destinations": {"whatsapp": "wa-1"}},
            )
        )
    return store


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_connected_tenant_sends_with_their_own_graph_token(live) -> None:
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = await _seed(admin_id, instance_id, material={"access_token": "tenant-token-1"})

    async with tenant_session(admin_id) as s:
        sender = await build_tenant_meta_sender(s, admin_id, instance_id, store=store)

    assert isinstance(sender, GraphMetaSender)
    assert sender._token == "tenant-token-1"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_tenant_with_no_meta_connection_gets_nothing_rather_than_a_platform_token(
    live,
) -> None:
    """The whole point: no connection must mean no sender, even in live mode."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()

    async with tenant_session(admin_id) as s:
        sender = await build_tenant_meta_sender(s, admin_id, instance_id, store=LocalSecretStore())

    assert isinstance(sender, NullMetaSender)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_connection_whose_secret_holds_no_token_is_not_a_sender(live) -> None:
    """A row can exist before the exchange lands (status unconfigured, empty material).
    Building a live sender off it would send with an empty token."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = await _seed(admin_id, instance_id, status="unconfigured", material={})

    async with tenant_session(admin_id) as s:
        sender = await build_tenant_meta_sender(s, admin_id, instance_id, store=store)

    assert isinstance(sender, NullMetaSender)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_revoking_the_connection_stops_the_sends(live) -> None:
    """Revoked rows are retained (§3.8.4) — retained must not mean usable."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = await _seed(
        admin_id, instance_id, status="revoked", material={"access_token": "revoked-token"}
    )

    async with tenant_session(admin_id) as s:
        sender = await build_tenant_meta_sender(s, admin_id, instance_id, store=store)

    assert isinstance(sender, NullMetaSender)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_one_tenants_token_never_builds_another_tenants_sender(live) -> None:
    admin_a, instance_a = uuid.uuid4(), uuid.uuid4()
    admin_b, instance_b = uuid.uuid4(), uuid.uuid4()
    store = await _seed(admin_a, instance_a, material={"access_token": "a-token"})
    await _seed(admin_b, instance_b, material=None)

    async with tenant_session(admin_b) as s:
        sender = await build_tenant_meta_sender(s, admin_b, instance_b, store=store)

    assert isinstance(sender, NullMetaSender)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_outside_live_mode_a_stored_token_still_never_reaches_the_network() -> None:
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = await _seed(admin_id, instance_id, material={"access_token": "tenant-token-1"})

    async with tenant_session(admin_id) as s:
        sender = await build_tenant_meta_sender(s, admin_id, instance_id, store=store)

    assert isinstance(sender, NullMetaSender)


async def test_the_platform_wide_meta_token_setting_is_gone() -> None:
    """Re-introducing this field is exactly how the platform-token path comes back."""
    from app.config import Settings

    assert "meta_access_token" not in Settings.model_fields
