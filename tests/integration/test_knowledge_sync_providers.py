"""Sync engine exercised against the REAL provider classes (not just the generic
FakeDriveProvider in tests/integration/test_knowledge_sync.py), with a fake
HttpTransport standing in for the network call (Build Brief rule 2: no real
network call in mock mode). Proves add/update/delete propagate identically
regardless of which concrete provider (HubSpot CRM-KB, website crawl) supplies
the snapshot — the sync engine only depends on the SyncProvider Protocol.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from app.db.session import tenant_session
from app.knowledge import service, sync
from app.knowledge.providers import HubSpotProvider, WebsiteCrawlProvider
from app.models.instance import Account, AccountState, Instance, LucielState

pytestmark = [pytest.mark.asyncio]


class FakeSecretStore:
    def __init__(self, material: dict[str, dict[str, str]] | None = None) -> None:
        self._data = material or {}

    async def put(self, ref, material):
        self._data[ref] = material

    async def get(self, ref):
        return self._data.get(ref)

    async def delete(self, ref):
        self._data.pop(ref, None)


class ScriptedTransport:
    """Replays a queue of canned responses, one per call — lets a test simulate a
    provider's data changing between two sync runs without touching real network."""

    def __init__(self, json_bodies: list[dict]) -> None:
        self._queue = list(json_bodies)

    async def get(self, url, *, headers=None, params=None):
        body = self._queue.pop(0)
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))


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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hubspot_provider_add_update_delete_via_sync_engine():
    """CRM-KB (HubSpot) real provider class, driven through the generic sync
    engine: article added, then updated (new updatedAt), then removed at source."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn_id = uuid.uuid4()
    from app.connections.secrets import secret_ref_for

    store = FakeSecretStore(
        {secret_ref_for(admin, iid, "knowledge_source"): {"access_token": "tok-hs"}}
    )

    transport = ScriptedTransport(
        [
            {
                "results": [
                    {"id": 1, "title": "Return policy", "postBody": "30 days", "updatedAt": "u1"},
                    {"id": 2, "title": "Warranty", "postBody": "1 year", "updatedAt": "u1"},
                ]
            }
        ]
    )
    provider = HubSpotProvider(admin_id=admin, instance_id=iid, store=store, transport=transport)

    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn_id,
            origin="crm_kb",
            provider=provider,
        )
        assert set(res.added) == {"1", "2"}

    # Article 1 updated (new updatedAt), article 2 removed at source.
    provider._transport = ScriptedTransport(
        [
            {
                "results": [
                    {
                        "id": 1,
                        "title": "Return policy",
                        "postBody": "60 days now",
                        "updatedAt": "u2",
                    },
                ]
            }
        ]
    )
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn_id,
            origin="crm_kb",
            provider=provider,
        )
        assert res.updated == ["1"]
        assert res.removed == ["2"]

    async with tenant_session(admin) as s:
        sources = await service.list_sources(s, admin)
        ext_ids = {x.external_id for x in sources}
        assert ext_ids == {"1"}
        results = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="60 days now", k=3
        )
        assert results and "60 days" in results[0][0].text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_website_crawl_provider_diff_via_sync_engine_no_oauth():
    """WebsiteCrawlProvider genuinely has no OAuth step — the sync engine reconciles
    purely off the content-hash revision computed from a fresh httpx fetch (faked
    here for determinism) diffed against the previous fetch."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn_id = uuid.uuid4()

    class FixedTextTransport:
        def __init__(self, text: str) -> None:
            self._text = text

        async def get(self, url, *, headers=None, params=None):
            return httpx.Response(200, text=self._text, request=httpx.Request("GET", url))

    provider = WebsiteCrawlProvider(
        urls=["https://example.com/faq"], transport=FixedTextTransport("<p>Hours: 9-5 weekdays</p>")
    )

    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn_id,
            origin="website_crawl",
            provider=provider,
        )
        assert res.added == ["https://example.com/faq"]

    # Re-run with IDENTICAL content: no update should be reported (hash unchanged).
    provider._transport = FixedTextTransport("<p>Hours: 9-5 weekdays</p>")
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn_id,
            origin="website_crawl",
            provider=provider,
        )
        assert res.added == [] and res.updated == [] and res.removed == []

    # Re-run with CHANGED content: reported as an update, chunks re-ingested.
    provider._transport = FixedTextTransport("<p>Hours: 8-6 every day now</p>")
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn_id,
            origin="website_crawl",
            provider=provider,
        )
        assert res.updated == ["https://example.com/faq"]

    async with tenant_session(admin) as s:
        results = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="8-6 every day now", k=3
        )
        assert results and "8-6" in results[0][0].text
