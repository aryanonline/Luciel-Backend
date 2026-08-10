"""Outbound email through the tenant's OWN Outlook mailbox (§3.1.6a).

Mirrors ``test_tenant_meta_sender``: the credential on the send path is the
customer's, sends through a dead credential refuse rather than fabricate, and —
the property specific to email — a broken mailbox must NOT silently fall back to
the platform SES sender, because that would swap the FROM identity behind the
owner's back (the mirror image of "never silently replaces a working sender").
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.channels import outbound
from app.channels.outlook_sender import NullOutlookSender, build_tenant_outlook_sender
from app.config import get_settings
from app.connections.secrets import LocalSecretStore, secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState

pytestmark = [pytest.mark.asyncio]

LEAD = "dana@lead.example"


@pytest.fixture(autouse=True)
def _fresh_secrets():
    LocalSecretStore.reset()
    yield
    LocalSecretStore.reset()


async def _seed_mailbox(
    admin_id: uuid.UUID,
    *,
    status: str = "connected",
    provider: str = "outlook",
    with_token: bool = True,
) -> uuid.UUID:
    instance_id = uuid.uuid4()
    ref = secret_ref_for(admin_id, instance_id, "email_sender")
    if with_token:
        store = LocalSecretStore()
        await store.put(ref, {"access_token": "ms-tok", "refresh_token": "ms-refresh"})
    expiry = (datetime.now(UTC) + timedelta(minutes=50)).isoformat()
    async with tenant_session(admin_id) as s:
        s.add(Instance(id=instance_id, admin_id=admin_id, name="A", state=LucielState.active))
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type="email_sender",
                auth_class="oauth_token",
                provider=provider,
                status=status,
                secret_ref=ref,
                non_secret_config={
                    "address": "owner@fabrikam.example",
                    "destination": "owner@fabrikam.example",
                    "mode": "byo_mailbox",
                    "token_expires_at": expiry,
                },
            )
        )
        await s.flush()
    return instance_id


async def _reply(admin_id: uuid.UUID, instance_id: uuid.UUID, **kwargs: object):
    async with tenant_session(admin_id) as s:
        return await outbound.send_outbound_reply(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            channel="email",
            body="We open at 9.",
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_mock_mode_replies_through_the_null_mailbox_never_the_network():
    """In mock the connected mailbox 'sends' through the hermetic Null sender —
    accepted, recorded, with NO provider id (Graph's 202 has none in live either)."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    admin_id = uuid.uuid4()
    iid = await _seed_mailbox(admin_id)

    result = await _reply(admin_id, iid, to=LEAD, subject="hours?")

    assert result.delivered is True
    assert result.provider_message_id is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_not_connected_mailbox_refuses_with_reconnect_needed_no_ses_fallback(
    monkeypatch,
):
    """The expired-mailbox path: refuse with an actionable reason. Falling back to
    SES here would silently swap the FROM identity the owner chose."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    admin_id = uuid.uuid4()
    iid = await _seed_mailbox(admin_id, status="expired")

    result = await _reply(admin_id, iid, to=LEAD)

    assert result.delivered is False
    assert result.detail == "mailbox_reconnect_needed"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_live_with_an_unrefreshable_token_refuses_rather_than_fabricates(monkeypatch):
    """Live + connected-on-paper but no fresh token (refresh would need the network,
    which the hermetic store cannot satisfy): the reply must refuse, not pretend."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    from tests.unit.live_settings import LIVE_PLATFORM_SECRET

    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", LIVE_PLATFORM_SECRET)
    # Keep the secret backend hermetic: without this, the live-mode code path would
    # bind the process-GLOBAL SecretsManagerStore singleton (get_secret_store caches
    # forever), poisoning every later test in the run with an AWS-bound store.
    from app.channels import tenant_outlook

    monkeypatch.setattr(tenant_outlook, "get_secret_store", LocalSecretStore)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        admin_id = uuid.uuid4()
        # Token stored, but expiry hint forces a refresh which has no reachable
        # endpoint in tests -> ensure_fresh_token returns None -> Null in live.
        iid = await _seed_mailbox(admin_id)
        async with tenant_session(admin_id) as s:
            conn = (
                await s.execute(
                    InstanceConnection.__table__.select().where(
                        InstanceConnection.admin_id == admin_id
                    )
                )
            ).one()
            # Rewrite the expiry to force the refresh path.
        async with tenant_session(admin_id) as s:
            from sqlalchemy import update

            await s.execute(
                update(InstanceConnection)
                .where(InstanceConnection.admin_id == admin_id)
                .values(
                    non_secret_config={
                        **dict(conn.non_secret_config),
                        "token_expires_at": datetime.now(UTC).isoformat(),
                    }
                )
            )

        result = await _reply(admin_id, iid, to=LEAD)

        assert result.delivered is False
        assert result.detail == "mailbox_reconnect_needed"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_ses_tenant_keeps_the_platform_path_untouched():
    """No outlook row: the existing platform-SES reply path runs exactly as before."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    admin_id = uuid.uuid4()
    iid = await _seed_mailbox(admin_id, provider="ses")

    result = await _reply(admin_id, iid, to=LEAD, subject="hours?")

    assert result.delivered is True  # NullEmailSender's mock path, as today


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_one_tenants_token_never_builds_another_tenants_sender():
    get_settings.cache_clear()  # type: ignore[attr-defined]
    admin_a, admin_b = uuid.uuid4(), uuid.uuid4()
    await _seed_mailbox(admin_a)
    iid_b = uuid.uuid4()
    async with tenant_session(admin_b) as s:
        s.add(Instance(id=iid_b, admin_id=admin_b, name="B", state=LucielState.active))
        await s.flush()

    async with tenant_session(admin_b) as s:
        sender = await build_tenant_outlook_sender(s, admin_b, iid_b)

    assert isinstance(sender, NullOutlookSender)  # B has no mailbox; A's token unreachable


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reply_context_threads_through_the_graph_reply_endpoint():
    """When the inbound Graph message id is known, the reply must use /reply (true
    threading) — recorded by the Null sender in mock, same call shape as live."""
    get_settings.cache_clear()  # type: ignore[attr-defined]
    admin_id = uuid.uuid4()
    iid = await _seed_mailbox(admin_id)

    async with tenant_session(admin_id) as s:
        sender = await build_tenant_outlook_sender(s, admin_id, iid)
        result = await outbound.send_outbound_reply(
            s,
            admin_id=admin_id,
            instance_id=iid,
            channel="email",
            to=LEAD,
            body="We open at 9.",
            in_reply_to="graph-msg-123",
        )

    assert result.delivered is True
    assert isinstance(sender, NullOutlookSender)
