"""Knowledge SCOPE selection is honoured by ingestion (Decision #9).

"Connect Google Drive" used to mean "read the whole account". A small-business owner's
Drive holds payroll and contracts next to the FAQ they wanted Luciel to answer from, so the
central assertion here is the negative one: once a scope is chosen, out-of-scope content is
NOT ingested — not on the first sync, and not on a later re-sync either.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from app.db.session import tenant_session
from app.knowledge import scope, service, sync
from app.knowledge.providers import GoogleDriveProvider, NotionProvider, provider_for_connection
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState

pytestmark = [pytest.mark.asyncio]


class FakeSecretStore:
    def __init__(self, material=None) -> None:
        self._data = material or {}

    async def put(self, ref, material):
        self._data[ref] = material

    async def get(self, ref):
        return self._data.get(ref)

    async def delete(self, ref):
        self._data.pop(ref, None)


class RecordingTransport:
    """Returns the SAME full listing regardless of the query, and records the params.

    Deliberate: a transport that honoured the ``q`` filter would let a provider that
    forgot to filter locally still look correct. By ignoring ``q`` this proves the
    provider enforces the scope on the results it actually receives — which is the only
    enforcement available for Notion, whose search API has no ancestor filter."""

    def __init__(self, body: dict) -> None:
        self._body = body
        self.calls: list[dict] = []

    async def get(self, url, *, headers=None, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        # Notion page bodies ride the blocks-children walk (Phase 8): empty is
        # fine here — this suite proves SCOPE enforcement, not text assembly.
        if "/v1/blocks/" in url:
            return httpx.Response(
                200, json={"results": [], "has_more": False}, request=httpx.Request("GET", url)
            )
        return httpx.Response(200, json=self._body, request=httpx.Request("GET", url))

    async def post(self, url, *, data=None, headers=None, json=None):
        # Notion search is a POST (Phase 8); same ignore-the-filter discipline.
        self.calls.append({"url": url, "json": dict(json or {})})
        return httpx.Response(200, json=self._body, request=httpx.Request("POST", url))


_DRIVE_BODY = {
    "files": [
        {"id": "f-faq", "name": "FAQ.doc", "modifiedTime": "r1", "parents": ["folder-public"]},
        {"id": "f-hours", "name": "Hours.doc", "modifiedTime": "r1", "parents": ["folder-public"]},
        {"id": "f-payroll", "name": "Payroll.xls", "modifiedTime": "r1", "parents": ["folder-hr"]},
        {"id": "f-loose", "name": "Loose.doc", "modifiedTime": "r1"},
    ]
}

_NOTION_BODY = {
    "results": [
        {"id": "p-faq", "title": "FAQ", "last_edited_time": "r1", "parent": {"page_id": "p-help"}},
        {"id": "p-help", "title": "Help centre", "last_edited_time": "r1"},
        {
            "id": "p-salary",
            "title": "Salary bands",
            "last_edited_time": "r1",
            "parent": {"page_id": "p-private"},
        },
    ]
}


async def _seed(admin_id: uuid.UUID) -> uuid.UUID:
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
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


def _store(admin_id, instance_id):
    from app.connections.secrets import secret_ref_for

    ref = secret_ref_for(admin_id, instance_id, "knowledge_source")
    return FakeSecretStore({ref: {"access_token": "tok"}})


# --- the scope record itself -------------------------------------------------- #


async def test_absent_scope_means_the_whole_account_not_nothing():
    """An existing unscoped connection must keep syncing everything — reading ``[]`` as
    "ingest nothing" would silently empty a working tenant's knowledge base."""

    class Row:
        non_secret_config = None

    assert scope.scope_ids(Row()) == []
    assert scope.drive_query([]) == "trashed=false"
    assert scope.in_scope({"id": "anything"}, [], parent_keys=("parents",)) is True


async def test_write_scope_reassigns_config_and_preserves_other_keys():
    class Row:
        non_secret_config = {"authorize_url": "https://x", "oauth_state_jti": None}

    row = Row()
    before = row.non_secret_config
    scope.write_scope(
        row, kind="drive_folders", selections=[{"id": "folder-public", "name": "Pub"}]
    )
    assert row.non_secret_config is not before, "must reassign so SQLAlchemy sees JSONB dirty"
    assert row.non_secret_config["authorize_url"] == "https://x"
    assert scope.scope_ids(row) == ["folder-public"]
    assert row.non_secret_config["scope"]["kind"] == "drive_folders"


async def test_drive_query_narrows_to_the_selected_folders():
    q = scope.drive_query(["folder-public", "folder-menus"])
    assert q == "trashed=false and ('folder-public' in parents or 'folder-menus' in parents)"


async def test_drive_query_refuses_to_interpolate_a_quote():
    """The ids come from a provider listing, but they reach the query as strings — a quote
    would break out of the literal, so such an id is dropped rather than embedded."""
    assert scope.drive_query(["bad'id"]) == "trashed=false"


# --- ingestion honours the scope --------------------------------------------- #


async def test_drive_snapshot_only_returns_in_scope_files():
    transport = RecordingTransport(_DRIVE_BODY)
    provider = GoogleDriveProvider(
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        store=FakeSecretStore(),
        transport=transport,
        scope_ids=["folder-public"],
    )
    # No secret store entry, so patch the token read the cheap way: seed the ref.
    from app.connections.secrets import secret_ref_for

    provider._store = FakeSecretStore(
        {
            secret_ref_for(provider._admin_id, provider._instance_id, "knowledge_source"): {
                "access_token": "t"
            }
        }
    )
    snap = await provider.snapshot(uuid.uuid4())
    assert {i.external_id for i in snap.items} == {"f-faq", "f-hours"}
    # The remote query was narrowed too, so out-of-scope content is not even fetched.
    assert "'folder-public' in parents" in transport.calls[0]["params"]["q"]
    assert "parents" in transport.calls[0]["params"]["fields"]


async def test_notion_snapshot_only_returns_in_scope_pages():
    from app.connections.secrets import secret_ref_for

    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    provider = NotionProvider(
        admin_id=admin_id,
        instance_id=instance_id,
        store=FakeSecretStore(
            {secret_ref_for(admin_id, instance_id, "knowledge_source"): {"access_token": "t"}}
        ),
        transport=RecordingTransport(_NOTION_BODY),
        scope_ids=["p-help"],
    )
    snap = await provider.snapshot(uuid.uuid4())
    # p-help is selected; p-faq is its child; p-salary lives under a page never selected.
    assert {i.external_id for i in snap.items} == {"p-help", "p-faq"}


async def test_unscoped_drive_snapshot_still_returns_everything():
    from app.connections.secrets import secret_ref_for

    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    provider = GoogleDriveProvider(
        admin_id=admin_id,
        instance_id=instance_id,
        store=FakeSecretStore(
            {secret_ref_for(admin_id, instance_id, "knowledge_source"): {"access_token": "t"}}
        ),
        transport=RecordingTransport(_DRIVE_BODY),
    )
    snap = await provider.snapshot(uuid.uuid4())
    assert len(snap.items) == 4


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_out_of_scope_content_is_never_ingested_as_a_knowledge_source():
    """End to end through the sync engine: only in-scope files become KnowledgeSource rows."""
    admin_id = uuid.uuid4()
    instance_id = await _seed(admin_id)
    connection_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            InstanceConnection(
                connection_id=connection_id,
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type="knowledge_source",
                auth_class="oauth_token",
                provider="google_drive",
                status="connected",
                non_secret_config={
                    "scope": {
                        "kind": "drive_folders",
                        "selections": [{"id": "folder-public", "name": "Public"}],
                    }
                },
            )
        )
        await s.flush()

    provider = GoogleDriveProvider(
        admin_id=admin_id,
        instance_id=instance_id,
        store=_store(admin_id, instance_id),
        transport=RecordingTransport(_DRIVE_BODY),
        scope_ids=["folder-public"],
    )
    async with tenant_session(admin_id) as s:
        result = await sync.sync_connection(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            connection_id=connection_id,
            origin="google_drive",
            provider=provider,
        )
        sources = await service.list_sources(s, admin_id)

    assert set(result.added) == {"f-faq", "f-hours"}
    names = {src.name for src in sources}
    assert names == {"FAQ.doc", "Hours.doc"}
    assert "Payroll.xls" not in names, "an out-of-scope file must never reach knowledge"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_narrowing_the_scope_removes_already_ingested_out_of_scope_sources():
    """Saving a narrower scope takes effect on the next sync, and the sync's own deletion
    propagation retires what fell outside it — the owner does not have to hand-delete."""
    admin_id = uuid.uuid4()
    instance_id = await _seed(admin_id)
    connection_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            InstanceConnection(
                connection_id=connection_id,
                admin_id=admin_id,
                instance_id=instance_id,
                connection_type="knowledge_source",
                auth_class="oauth_token",
                provider="google_drive",
                status="connected",
            )
        )
        await s.flush()

    store = _store(admin_id, instance_id)

    async def _sync_once(scope_ids):
        provider = GoogleDriveProvider(
            admin_id=admin_id,
            instance_id=instance_id,
            store=store,
            transport=RecordingTransport(_DRIVE_BODY),
            scope_ids=scope_ids,
        )
        async with tenant_session(admin_id) as s:
            return await sync.sync_connection(
                s,
                admin_id=admin_id,
                instance_id=instance_id,
                connection_id=connection_id,
                origin="google_drive",
                provider=provider,
            )

    first = await _sync_once([])  # unscoped: everything
    assert len(first.added) == 4

    second = await _sync_once(["folder-public"])  # owner narrows the scope
    assert set(second.removed) == {"f-payroll", "f-loose"}

    async with tenant_session(admin_id) as s:
        remaining = {src.name for src in await service.list_sources(s, admin_id)}
    assert remaining == {"FAQ.doc", "Hours.doc"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_provider_for_connection_reads_the_scope_off_the_row():
    """Every sync path goes through this factory, so the scope cannot be forgotten by a
    caller that builds a provider itself."""
    admin_id = uuid.uuid4()
    instance_id = await _seed(admin_id)
    async with tenant_session(admin_id) as s:
        conn = InstanceConnection(
            connection_id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type="knowledge_source",
            auth_class="oauth_token",
            provider="notion",
            status="connected",
            non_secret_config={
                "scope": {"kind": "notion_pages", "selections": [{"id": "p-help", "name": "Help"}]}
            },
        )
        s.add(conn)
        await s.flush()
        provider = provider_for_connection(conn, FakeSecretStore())
    assert isinstance(provider, NotionProvider)
    assert provider._scope_ids == ["p-help"]
