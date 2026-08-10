"""Microsoft token refresh: the first REAL refresher behind the §3.8.5 seam.

Microsoft access tokens live ~1 hour and Microsoft ROTATES refresh tokens, so the
two properties under test are: (1) a rotation that is not persisted strands the row
at the NEXT refresh (the old refresh token is burnt server-side the moment the new
one is issued), and (2) a refresh that cannot happen must surface as an honest
``None`` — the caller refuses, the health worker marks ``expired`` — never a token
that "probably still works".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.channels.tenant_outlook import (
    TOKEN_ENDPOINT,
    MicrosoftTokenRefresher,
    ProviderDispatchingRefresher,
    ensure_fresh_token,
)
from app.config import Settings, get_settings
from app.connections import health
from app.connections.secrets import LocalSecretStore, secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_secrets():
    LocalSecretStore.reset()
    yield
    LocalSecretStore.reset()


@pytest.fixture
def microsoft_client(monkeypatch):
    """A configured Entra app, visible to the registry's settings read."""
    from app.connections import registry as registry_module

    settings = Settings(
        microsoft_client_id="ms-cid",
        microsoft_client_secret="ms-csecret",  # noqa: S106 — test fixture
        microsoft_redirect_uri="https://api.example.com/api/v1/connections/microsoft/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    return settings


class FakeTokenEndpoint:
    def __init__(self, *, fail: bool = False, rotate_to: str = "ms-refresh-2") -> None:
        self.fail = fail
        self.rotate_to = rotate_to
        self.calls: list[dict[str, str]] = []

    async def post(self, url, *, data=None, headers=None):
        self.calls.append(dict(data or {}))
        assert url == TOKEN_ENDPOINT
        if self.fail:
            return httpx.Response(
                400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url)
            )
        return httpx.Response(
            200,
            json={
                "access_token": "ms-tok-NEW",
                "refresh_token": self.rotate_to,
                "expires_in": 3599,
            },
            request=httpx.Request("POST", url),
        )


async def _seed_outlook_row(
    admin_id: uuid.UUID, *, expires_at: datetime | None, store: LocalSecretStore
) -> uuid.UUID:
    instance_id = uuid.uuid4()
    ref = secret_ref_for(admin_id, instance_id, "email_sender")
    await store.put(ref, {"access_token": "ms-tok-OLD", "refresh_token": "ms-refresh-1"})
    cfg: dict[str, object] = {
        "address": "owner@fabrikam.example",
        "destination": "owner@fabrikam.example",
        "mode": "byo_mailbox",
    }
    if expires_at is not None:
        cfg["token_expires_at"] = expires_at.isoformat()
    async with tenant_session(admin_id) as s:
        s.add(Instance(id=instance_id, admin_id=admin_id, name="A", state=LucielState.active))
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type="email_sender",
                auth_class="oauth_token",
                provider="outlook",
                status="connected",
                secret_ref=ref,
                non_secret_config=cfg,
            )
        )
        await s.flush()
    return instance_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_far_from_expiry_token_is_returned_without_a_refresh(microsoft_client):
    admin_id = uuid.uuid4()
    store = LocalSecretStore()
    iid = await _seed_outlook_row(admin_id, expires_at=NOW + timedelta(minutes=50), store=store)
    endpoint = FakeTokenEndpoint()

    async with tenant_session(admin_id) as s:
        token = await ensure_fresh_token(s, admin_id, iid, store=store, transport=endpoint, now=NOW)

    assert token == "ms-tok-OLD"
    assert endpoint.calls == []  # no needless network round-trip per send/poll


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_near_expiry_refreshes_and_persists_the_rotation(microsoft_client):
    """Microsoft burns the old refresh token at rotation — persisting the new one is
    the difference between a working mailbox and one that dies at the NEXT refresh."""
    admin_id = uuid.uuid4()
    store = LocalSecretStore()
    iid = await _seed_outlook_row(admin_id, expires_at=NOW + timedelta(minutes=2), store=store)
    endpoint = FakeTokenEndpoint()

    async with tenant_session(admin_id) as s:
        token = await ensure_fresh_token(s, admin_id, iid, store=store, transport=endpoint, now=NOW)
        row = (
            await s.execute(
                InstanceConnection.__table__.select().where(InstanceConnection.admin_id == admin_id)
            )
        ).one()

    assert token == "ms-tok-NEW"
    material = await store.get(secret_ref_for(admin_id, iid, "email_sender"))
    assert material["access_token"] == "ms-tok-NEW"
    assert material["refresh_token"] == "ms-refresh-2"  # the rotation, persisted
    new_hint = row.non_secret_config["token_expires_at"]
    assert datetime.fromisoformat(new_hint) > NOW + timedelta(minutes=30)
    # The grant went to Microsoft with the platform client + the stored refresh token.
    call = endpoint.calls[0]
    assert call["grant_type"] == "refresh_token"
    assert call["refresh_token"] == "ms-refresh-1"
    assert call["client_id"] == "ms-cid"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_missing_expiry_hint_is_treated_as_stale_not_immortal(microsoft_client):
    admin_id = uuid.uuid4()
    store = LocalSecretStore()
    iid = await _seed_outlook_row(admin_id, expires_at=None, store=store)
    endpoint = FakeTokenEndpoint()

    async with tenant_session(admin_id) as s:
        token = await ensure_fresh_token(s, admin_id, iid, store=store, transport=endpoint, now=NOW)

    assert token == "ms-tok-NEW"
    assert len(endpoint.calls) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_rejected_refresh_returns_none_and_keeps_the_old_material(microsoft_client):
    """The caller must hear "cannot refresh", and the stored material must not be
    half-overwritten by a failed attempt."""
    admin_id = uuid.uuid4()
    store = LocalSecretStore()
    iid = await _seed_outlook_row(admin_id, expires_at=NOW - timedelta(minutes=1), store=store)

    async with tenant_session(admin_id) as s:
        token = await ensure_fresh_token(
            s, admin_id, iid, store=store, transport=FakeTokenEndpoint(fail=True), now=NOW
        )

    assert token is None
    material = await store.get(secret_ref_for(admin_id, iid, "email_sender"))
    assert material["access_token"] == "ms-tok-OLD"
    assert material["refresh_token"] == "ms-refresh-1"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_health_sweep_rotates_a_near_expiry_mailbox_and_stays_connected(
    microsoft_client,
):
    """§3.8.5 steps 3–4 through the real worker: proactive refresh inside the 30-min
    lead, new expiry hint persisted, status still connected — the owner never sees a
    blip for a routine rotation."""
    admin_id = uuid.uuid4()
    store = LocalSecretStore()
    iid = await _seed_outlook_row(admin_id, expires_at=NOW + timedelta(minutes=10), store=store)
    endpoint = FakeTokenEndpoint()

    async with tenant_session(admin_id) as s:
        statuses = await health.run_health_sweep(
            s,
            admin_id=admin_id,
            probe=health.StoredCredentialProbe(),
            refresher=MicrosoftTokenRefresher(transport_factory=lambda: endpoint),
            store=store,
            now=NOW,
        )

    assert list(statuses.values()) == ["connected"]
    material = await store.get(secret_ref_for(admin_id, iid, "email_sender"))
    assert material["refresh_token"] == "ms-refresh-2"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_sweep_refresh_marks_the_mailbox_expired_with_a_reason(
    microsoft_client,
):
    admin_id = uuid.uuid4()
    store = LocalSecretStore()
    await _seed_outlook_row(admin_id, expires_at=NOW + timedelta(minutes=10), store=store)

    async with tenant_session(admin_id) as s:
        statuses = await health.run_health_sweep(
            s,
            admin_id=admin_id,
            probe=health.StoredCredentialProbe(),
            refresher=MicrosoftTokenRefresher(
                transport_factory=lambda: FakeTokenEndpoint(fail=True)
            ),
            store=store,
            now=NOW,
        )
        row = (
            await s.execute(
                InstanceConnection.__table__.select().where(InstanceConnection.admin_id == admin_id)
            )
        ).one()

    assert list(statuses.values()) == ["expired"]
    assert "reconnect" in (row.status_detail or "")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_dispatching_refresher_keeps_mock_hermetic():
    """In mock mode even an outlook row must take the fallback (no network); the
    Microsoft path is live-gated. Everything non-outlook always takes the fallback."""
    get_settings.cache_clear()  # type: ignore[attr-defined]

    class RecordingFallback:
        def __init__(self) -> None:
            self.rows: list[str] = []

        async def refresh(self, connection, store):
            self.rows.append(connection.provider)
            return None

    fallback = RecordingFallback()
    dispatcher = ProviderDispatchingRefresher(fallback=fallback)
    outlook_row = InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_type="email_sender",
        auth_class="oauth_token",
        provider="outlook",
        status="connected",
        secret_ref="vm/x",
    )
    hubspot_row = InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_type="crm",
        auth_class="oauth_token",
        provider="hubspot",
        status="connected",
        secret_ref="vm/y",
    )

    assert await dispatcher.refresh(outlook_row, LocalSecretStore()) is None
    assert await dispatcher.refresh(hubspot_row, LocalSecretStore()) is None
    assert fallback.rows == ["outlook", "hubspot"]  # both fell back — mock stayed offline
