"""The manual knowledge sync is a JOB (round 7, live walk run 8, 2026-10-25).

"Crawl a website" ran inside the owner's request. The marketing site had grown to 8
pages: fetched in one second, then one embeddings call and one LLM graph call (4-16 s)
per page. The load balancer's 60 s idle timeout cut the request with a 504 of its own
(no CORS headers: the browser read "Network request failed.") while the sync kept
working server-side and finished with nobody listening.

The contract pinned here:
  - a sync that finishes within ``knowledge_sync_inline_wait_seconds`` answers exactly
    as before: 200 ``SyncResultOut``, or the same 409s (shrink, unreachable);
  - one still running answers 202 ``{connectionId, state: "syncing", startedAt, ...}``
    and its outcome lands on the connection's ``syncJob`` (GET /sync-connections and
    GET /connections) — synced with the result, failed with the reason, or
    shrink_pending with the counts;
  - a second start while a fresh job is ``syncing`` is a 409;
  - a ``syncing`` older than ``knowledge_sync_job_stale_seconds`` reads as failed;
  - a shutdown that cancels a running job stamps it "interrupted by a restart".

The provider is a fake whose snapshot awaits ``asyncio.sleep``; the inline wait is
monkeypatched down so a "slow" sync takes well under a second.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import get_settings
from app.knowledge import sync_job
from app.knowledge.providers import CrawlUnreachable
from app.knowledge.sync import RemoteItem, RemoteSnapshot

from tests.contract.test_dashboard import _signup_verify_login

pytestmark = [
    pytest.mark.contract,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset"),
]

SITE = "https://acme.example"


@dataclass
class _Plan:
    pages: list[str] = field(default_factory=lambda: ["/"])
    delay: float = 0.0
    error: Exception | None = None
    calls: int = 0


class _FakeCrawl:
    origin = "website_crawl"

    def __init__(self, plan: _Plan) -> None:
        self._plan = plan

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        self._plan.calls += 1
        await asyncio.sleep(self._plan.delay)  # the per-page embeddings + LLM graph cost
        if self._plan.error is not None:
            raise self._plan.error
        return RemoteSnapshot(
            items=[
                RemoteItem(
                    external_id=f"{SITE}{p}",
                    name=f"{SITE}{p}",
                    text=f"The page {p} says we are open nine to five on weekdays.",
                    revision="r1",
                )
                for p in self._plan.pages
            ]
        )


@pytest_asyncio.fixture
async def api(client, monkeypatch) -> AsyncIterator[tuple[object, _Plan]]:
    """The client with the crawl provider faked; every job is drained before the
    client's engine is disposed."""
    import app.api.v1.admin.knowledge as knowledge_api

    plan = _Plan()
    monkeypatch.setattr(knowledge_api, "provider_for_connection", lambda c, s: _FakeCrawl(plan))
    yield client, plan
    await sync_job.drain_sync_jobs(timeout=10)


def _inline_wait(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(get_settings(), "knowledge_sync_inline_wait_seconds", seconds)


async def _ready(client) -> tuple[uuid.UUID, str]:
    admin = await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": [f"{SITE}/"]},
    )
    assert rc.status_code == 201, rc.text
    return admin, rc.json()["connectionId"]


async def _sync(client, cid: str, *, confirm: bool = False):
    q = "?confirmShrink=true" if confirm else ""
    return await client.post(f"/api/v1/admin/knowledge/sync-connections/{cid}/sync{q}")


async def _finish(cid: str) -> None:
    task = sync_job._RUNNING.get(uuid.UUID(cid))
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=15)


async def _job(client, cid: str) -> dict | None:
    r = await client.get("/api/v1/admin/knowledge/sync-connections")
    assert r.status_code == 200
    row = next(c for c in r.json() if c["connectionId"] == cid)
    return row["syncJob"]


async def test_a_fast_sync_still_answers_200_and_the_row_records_it(api):
    client, plan = api
    _admin, cid = await _ready(client)
    assert await _job(client, cid) is None  # never synced: no job, not a fake "synced"
    plan.pages = ["/", "/contact"]

    r = await _sync(client, cid)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"added", "updated", "removed", "refused"}  # unchanged shape
    assert sorted(r.json()["added"]) == [f"{SITE}/", f"{SITE}/contact"]

    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "synced"
    assert job["trigger"] == "manual"
    assert job["startedAt"] and job["finishedAt"]
    assert job["error"] is None and job["shrink"] is None
    assert sorted(job["result"]["added"]) == [f"{SITE}/", f"{SITE}/contact"]
    assert set(job["result"]) == {"added", "updated", "removed", "refused"}


async def test_a_slow_sync_answers_202_and_the_row_then_reads_synced(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    plan.delay = 0.6

    r = await _sync(client, cid)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body == {
        "connectionId": cid,
        "state": "syncing",
        "startedAt": body["startedAt"],
        "finishedAt": None,
        "result": None,
        "error": None,
        "shrink": None,
        "trigger": "manual",
    }
    assert body["startedAt"]
    # Pending is never shown as success: the row says "syncing" while the job runs.
    running = await _job(client, cid)
    assert running is not None and running["state"] == "syncing"
    assert running["result"] is None

    await _finish(cid)
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "synced"
    assert job["result"]["added"] == [f"{SITE}/"]
    assert job["startedAt"] == body["startedAt"]
    assert job["finishedAt"]


async def test_a_second_start_while_syncing_is_a_409(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    plan.delay = 0.6

    assert (await _sync(client, cid)).status_code == 202
    again = await _sync(client, cid)
    assert again.status_code == 409
    assert again.json()["message"] == sync_job.ALREADY_RUNNING
    assert plan.calls == 1  # the refused start ran nothing

    await _finish(cid)
    plan.delay = 0.0
    _inline_wait(monkeypatch, 20.0)
    assert (await _sync(client, cid)).status_code == 200  # finished: free to start again


async def test_a_fresh_syncing_row_from_another_process_also_refuses(api):
    client, plan = api
    admin, cid = await _ready(client)
    stamp = sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="scheduled")
    assert await sync_job.stamp_job(admin, uuid.UUID(cid), stamp)

    r = await _sync(client, cid)
    assert r.status_code == 409
    assert r.json()["message"] == sync_job.ALREADY_RUNNING
    assert plan.calls == 0


async def test_a_stale_syncing_reads_failed_and_does_not_block_a_new_start(api, monkeypatch):
    client, _plan = api
    admin, cid = await _ready(client)
    stale_after = get_settings().knowledge_sync_job_stale_seconds
    old = datetime.now(UTC) - timedelta(seconds=stale_after + 60)
    stamp = sync_job.syncing_stamp(started_at=old, trigger="manual")
    assert await sync_job.stamp_job(admin, uuid.UUID(cid), stamp)

    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "failed"
    assert job["error"] == "The sync was interrupted before it finished — try again."
    assert job["result"] is None

    # Read-time only: the stored record keeps its evidence.
    raw = dict(stamp)
    assert sync_job.effective_job_record(raw)["state"] == "failed"
    assert raw["state"] == "syncing"
    fresh = sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual")
    assert sync_job.effective_job_record(fresh)["state"] == "syncing"

    r = await _sync(client, cid)
    assert r.status_code == 200, r.text
    assert (await _job(client, cid))["state"] == "synced"


async def test_a_shrink_is_409_inline_and_shrink_pending_deferred_then_confirm_completes(
    api, monkeypatch
):
    client, plan = api
    _admin, cid = await _ready(client)
    plan.pages = ["/", "/a", "/b", "/c"]
    assert (await _sync(client, cid)).status_code == 200

    message = (
        "The source now reports 1 item(s) where 4 are synced, so nothing was removed. "
        "If you did remove them at the source, sync again and confirm the shrink."
    )
    plan.pages = ["/"]
    inline = await _sync(client, cid)
    assert inline.status_code == 409
    assert inline.json()["message"] == message
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "shrink_pending"
    assert job["shrink"] == {"remote": 1, "existing": 4}
    assert job["error"] == message

    _inline_wait(monkeypatch, 0.05)
    plan.delay = 0.4
    deferred = await _sync(client, cid)
    assert deferred.status_code == 202
    await _finish(cid)
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "shrink_pending"
    assert job["shrink"] == {"remote": 1, "existing": 4}
    assert job["error"] == message
    assert job["result"] is None

    _inline_wait(monkeypatch, 20.0)
    plan.delay = 0.0
    confirmed = await _sync(client, cid, confirm=True)
    assert confirmed.status_code == 200, confirmed.text
    assert sorted(confirmed.json()["removed"]) == [f"{SITE}/a", f"{SITE}/b", f"{SITE}/c"]
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "synced"
    assert job["shrink"] is None and job["error"] is None


async def test_an_unreachable_site_is_the_same_409_inline_and_failed_deferred(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    plan.error = CrawlUnreachable(f"crawl fetched no pages: {SITE}/")
    message = f"Sync unavailable: crawl fetched no pages: {SITE}/"

    inline = await _sync(client, cid)
    assert inline.status_code == 409
    assert inline.json()["message"] == message
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "failed" and job["error"] == message

    _inline_wait(monkeypatch, 0.05)
    plan.delay = 0.4
    deferred = await _sync(client, cid)
    assert deferred.status_code == 202
    await _finish(cid)
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "failed"
    assert job["error"] == message
    assert job["result"] is None
    # A crawl holds no credential: the connection keeps its status (knowledge #2).
    rows = (await client.get("/api/v1/admin/knowledge/sync-connections")).json()
    assert next(c for c in rows if c["connectionId"] == cid)["status"] == "connected"


async def test_any_other_failure_is_a_503_with_the_owner_message_never_a_bare_500(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    plan.error = RuntimeError("internal detail that must not reach the owner")

    r = await _sync(client, cid)
    assert r.status_code == 503
    assert r.json()["message"] == sync_job.FAILED_GENERIC
    assert "internal detail" not in r.text
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "failed" and job["error"] == sync_job.FAILED_GENERIC

    _inline_wait(monkeypatch, 0.05)
    plan.delay = 0.3
    assert (await _sync(client, cid)).status_code == 202
    await _finish(cid)
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "failed" and job["error"] == sync_job.FAILED_GENERIC


async def test_a_shutdown_mid_job_stamps_interrupted_by_a_restart(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    plan.delay = 30.0

    assert (await _sync(client, cid)).status_code == 202
    await sync_job.drain_sync_jobs(timeout=0.05)
    assert uuid.UUID(cid) not in sync_job._RUNNING
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "failed"
    assert job["error"] == "The sync was interrupted by a restart — try again."


async def test_get_connections_and_get_sync_connections_both_carry_sync_job(api):
    client, _plan = api
    _admin, cid = await _ready(client)

    before = (await client.get("/api/v1/admin/connections")).json()
    row = next(c for c in before if c["connectionId"] == cid)
    assert "syncJob" in row and row["syncJob"] is None

    assert (await _sync(client, cid)).status_code == 200
    generic = next(
        c
        for c in (await client.get("/api/v1/admin/connections")).json()
        if c["connectionId"] == cid
    )
    knowledge = await _job(client, cid)
    assert generic["syncJob"] is not None
    assert generic["syncJob"]["state"] == "synced"
    assert generic["syncJob"] == knowledge  # one record, two readers
    assert set(generic["syncJob"]) == {
        "state",
        "startedAt",
        "finishedAt",
        "result",
        "error",
        "shrink",
        "trigger",
    }
