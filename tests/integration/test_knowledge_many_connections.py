"""Knowledge sources are MANY per tenant (2026-09-05 audit F092/F093/F094).

A business has a Drive folder AND a Notion workspace AND its website. The
single-active-per-type rule that is right for a sender or a CRM (§3.8.2) used to
reset the Drive row to ``unconfigured`` — and point Notion's token at the same
secret ref — the moment the owner clicked "connect Notion". Now each knowledge
connection is its own row with its own secret ref, and a sync reconciles only the
sources that belong to that connection.
"""

from __future__ import annotations

import uuid

import pytest
from app.connections import service as connections_service
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.knowledge import providers, sync
from app.knowledge.sync import RemoteItem, RemoteSnapshot
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeSource
from sqlalchemy import select


class FakeSecretStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}

    async def put(self, ref, material):
        self.data[ref] = dict(material)

    async def get(self, ref):
        return self.data.get(ref)

    async def delete(self, ref):
        return self.data.pop(ref, None) is not None


class StaticProvider:
    origin = "google_drive"

    def __init__(self, items: list[RemoteItem]) -> None:
        self._items = items

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        return RemoteSnapshot(items=list(self._items))


async def _seed(admin: uuid.UUID) -> uuid.UUID:
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
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


async def _start(admin: uuid.UUID, iid: uuid.UUID, provider: str) -> InstanceConnection:
    async with tenant_session(admin) as s:
        conn, _ = await connections_service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type="knowledge_source",
            provider=provider,
            user_id=admin,
        )
        await s.refresh(conn)
        return conn


async def _rows(admin: uuid.UUID) -> list[InstanceConnection]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(InstanceConnection)
                    .where(
                        InstanceConnection.admin_id == admin,
                        InstanceConnection.connection_type == "knowledge_source",
                    )
                    .order_by(InstanceConnection.created_at)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connecting_a_second_knowledge_source_keeps_the_first_and_its_own_secret():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    drive = await _start(admin, iid, "google_drive")
    async with tenant_session(admin) as s:
        row = await s.get(InstanceConnection, drive.connection_id)
        assert row is not None
        row.status = "connected"  # the Drive consent completed
    notion = await _start(admin, iid, "notion")

    rows = await _rows(admin)
    assert [r.provider for r in rows] == ["google_drive", "notion"]
    assert rows[0].status == "connected"  # untouched by the second connect
    assert rows[1].status == "unconfigured"
    assert rows[0].secret_ref != rows[1].secret_ref
    legacy_shared = secret_ref_for(admin, iid, "knowledge_source")
    assert rows[0].secret_ref.startswith(legacy_shared + "/")
    assert rows[1].secret_ref.startswith(legacy_shared + "/")
    assert notion.connection_id != drive.connection_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_abandoned_connect_of_the_same_provider_is_reused_not_multiplied():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    first = await _start(admin, iid, "notion")
    second = await _start(admin, iid, "notion")  # the owner clicked Connect twice
    assert second.connection_id == first.connection_id
    assert len(await _rows(admin)) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_tokens_live_under_each_connections_own_ref():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    drive = await _start(admin, iid, "google_drive")
    hub = await _start(admin, iid, "hubspot")
    store = FakeSecretStore()
    await providers.store_oauth_token(
        store,
        admin_id=admin,
        instance_id=iid,
        material={"access_token": "drive-token"},
        secret_ref=drive.secret_ref,
    )
    await providers.store_oauth_token(
        store,
        admin_id=admin,
        instance_id=iid,
        material={"access_token": "hub-token"},
        secret_ref=hub.secret_ref,
    )
    assert store.data[drive.secret_ref]["access_token"] == "drive-token"
    assert store.data[hub.secret_ref]["access_token"] == "hub-token"
    # Each provider reads ITS row's ref — never the other's, never the shared legacy one.
    assert await providers._bearer_token(store, admin, iid, drive.secret_ref) == "drive-token"
    assert await providers._bearer_token(store, admin, iid, hub.secret_ref) == "hub-token"
    with pytest.raises(providers.ProviderUnavailable):
        await providers._bearer_token(store, admin, iid)  # nothing at the shared ref


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_syncing_one_connection_never_retires_anothers_sources():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    drive = await _start(admin, iid, "google_drive")
    notion = await _start(admin, iid, "notion")

    async with tenant_session(admin) as s:
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=drive.connection_id,
            origin="google_drive",
            provider=StaticProvider([RemoteItem("d1", "Price list", "Decks from $40/sqft", "r1")]),
        )
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=notion.connection_id,
            origin="notion",
            provider=StaticProvider([RemoteItem("n1", "FAQ", "We are open 9-5", "r1")]),
        )
    # Drive's next sync reports the same single file: Notion's source must survive.
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=drive.connection_id,
            origin="google_drive",
            provider=StaticProvider([RemoteItem("d1", "Price list", "Decks from $40/sqft", "r1")]),
        )
        assert res.removed == []
        live = (
            (
                await s.execute(
                    select(KnowledgeSource).where(
                        KnowledgeSource.admin_id == admin,
                        KnowledgeSource.soft_deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(src.external_id for src in live) == ["d1", "n1"]
    assert {src.connection_id for src in live} == {drive.connection_id, notion.connection_id}
    # And the pillar's raw view lists both connections.
    assert [r.provider for r in await _rows(admin)] == ["google_drive", "notion"]
