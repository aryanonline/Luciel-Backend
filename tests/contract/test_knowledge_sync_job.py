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
  - liveness is a database lock, not an age: a job past any age reads syncing and a
    second start is a 409; a row with no lock past the grace reads interrupted and a
    start is admitted at once;
  - data, terminal stamp and lock release become visible together, and the stamp never
    overwrites a record it does not own;
  - removing a source stops its sync: nothing kept, the row serves no job;
  - the per-source re-sync is the same job;
  - a shutdown that cancels a running job stamps it "interrupted by a restart".

The provider is a fake. A "slow" sync is one whose snapshot a ``_Gate`` holds until the
request under test has answered, never a fixed sleep; the inline wait is monkeypatched
down so a held job answers 202 at once. Only the shutdown tests still sleep (30 s): their
job is cancelled long before that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import get_settings
from app.db.session import tenant_session
from app.knowledge import service, sync, sync_job
from app.knowledge.embeddings import EMBEDDING_DOWN, EmbeddingError
from app.knowledge.graph_extract import ExtractionResult, LlmGraphExtractor
from app.knowledge.providers import CrawlUnreachable, ProviderUnavailable
from app.knowledge.sync import RemoteItem, RemoteSnapshot
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState
from app.models.knowledge import KnowledgeSource
from sqlalchemy import select, update

from tests.contract.test_dashboard import _signup_verify_login

pytestmark = [
    pytest.mark.contract,
    pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset"),
]

SITE = "https://acme.example"


@dataclass
class _Plan:
    pages: list[str] = field(default_factory=lambda: ["/"])
    delay: float = 0.0
    error: Exception | None = None
    calls: int = 0
    revision: str = "r1"
    text: str = "says we are open nine to five on weekdays."
    failed_ids: list[str] = field(default_factory=list)
    unreached_ids: list[str] = field(default_factory=list)
    during_snapshot: Callable[[], Awaitable[None]] | None = None


class _FakeCrawl:
    origin = "website_crawl"

    def __init__(self, plan: _Plan) -> None:
        self._plan = plan

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        self._plan.calls += 1
        await asyncio.sleep(self._plan.delay)  # the per-page embeddings + LLM graph cost
        if self._plan.during_snapshot is not None:
            await self._plan.during_snapshot()
        if self._plan.error is not None:
            raise self._plan.error
        return RemoteSnapshot(
            items=[
                RemoteItem(
                    external_id=f"{SITE}{p}",
                    name=f"{SITE}{p}",
                    text=f"The page {p} {self._plan.text}",
                    revision=self._plan.revision,
                )
                for p in self._plan.pages
            ],
            failed_ids=self._plan.failed_ids,
            unreached_ids=self._plan.unreached_ids,
        )


#: Bound on every wait of a ``_Gate``: far longer than any test holds one, so a gate that
#: is never reached or never released fails the test instead of hanging the run.
_GATE_SECONDS = 10


class _Gate:
    """Holds the fake snapshot until the test releases it.

    A fixed ``plan.delay`` only bets that the request under test lands while the job is
    still in its snapshot. On a slow runner the job can commit first: CI once kept the
    synced page of a source whose disconnect answered after the job's commit. Installed
    by ``_hold_snapshots``, the gate records that the job is inside its snapshot (its
    transaction open, its lock held) and lets that snapshot, and every later one,
    return only after ``release``."""

    def __init__(self) -> None:
        self._entered = asyncio.Event()
        self._released = asyncio.Event()

    async def hold(self) -> None:
        self._entered.set()
        await asyncio.wait_for(self._released.wait(), timeout=_GATE_SECONDS)

    async def reached(self) -> None:
        """Return once the job is inside its snapshot."""
        await asyncio.wait_for(self._entered.wait(), timeout=_GATE_SECONDS)

    async def release(self) -> None:
        """Let the held snapshot return. The job must have reached it first, so a gate the
        job never met fails here instead of holding nothing."""
        await self.reached()
        self._released.set()


def _hold_snapshots(plan: _Plan) -> _Gate:
    gate = _Gate()
    plan.during_snapshot = gate.hold
    return gate


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


async def _raw_job(admin: uuid.UUID, cid: str) -> dict | None:
    """The stored column, not the served reading."""
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(InstanceConnection.sync_job).where(
                    InstanceConnection.connection_id == uuid.UUID(cid)
                )
            )
        ).scalar_one_or_none()


async def _seed_job(admin: uuid.UUID, cid: str, record: dict | None, **columns: object) -> None:
    async with tenant_session(admin) as s:
        await s.execute(
            update(InstanceConnection)
            .where(InstanceConnection.connection_id == uuid.UUID(cid))
            .values(sync_job=record, **columns)
        )


async def _sources_of(admin: uuid.UUID, cid: str) -> list[KnowledgeSource]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(KnowledgeSource).where(
                        KnowledgeSource.connection_id == uuid.UUID(cid),
                        KnowledgeSource.soft_deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )


@contextlib.asynccontextmanager
async def _held_lock(admin: uuid.UUID, cid: str) -> AsyncIterator[None]:
    """Another process's running job: its lock, held by an open transaction."""
    async with tenant_session(admin) as s:
        assert await sync.claim_sync_lock(s, uuid.UUID(cid))
        yield


async def _disconnect(client, cid: str):
    return await client.post(f"/api/v1/admin/connections/{cid}/disconnect")


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
    assert set(job["result"]) == {"added", "updated", "removed", "refused", "halted"}
    assert job["result"]["halted"] is None


async def test_a_slow_sync_answers_202_and_the_row_then_reads_synced(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    gate = _hold_snapshots(plan)

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

    await gate.release()
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
    gate = _hold_snapshots(plan)

    assert (await _sync(client, cid)).status_code == 202
    await gate.reached()  # the first job is in its snapshot: its one provider call is made
    again = await _sync(client, cid)
    assert again.status_code == 409
    assert again.json()["message"] == sync_job.ALREADY_RUNNING
    assert plan.calls == 1  # the refused start ran nothing

    await gate.release()
    await _finish(cid)
    _inline_wait(monkeypatch, 20.0)
    assert (await _sync(client, cid)).status_code == 200  # finished: free to start again


async def test_a_syncing_row_inside_the_start_grace_also_refuses(api):
    client, plan = api
    admin, cid = await _ready(client)
    stamp = sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="scheduled")
    await _seed_job(admin, cid, stamp)

    r = await _sync(client, cid)
    assert r.status_code == 409
    assert r.json()["message"] == sync_job.ALREADY_RUNNING
    assert plan.calls == 0
    assert (await _job(client, cid))["state"] == "syncing"


async def test_a_job_past_any_age_reads_syncing_while_its_lock_is_held(api):
    """The consequence the age rule got wrong: a 20-minute Drive sync read "failed" and a
    second reconcile could start beside it. The lock says it is alive, whatever its age."""
    client, plan = api
    admin, cid = await _ready(client)
    ancient = datetime.now(UTC) - timedelta(days=3)
    stamp = sync_job.syncing_stamp(started_at=ancient, trigger="scheduled")
    await _seed_job(admin, cid, stamp)

    async with _held_lock(admin, cid):
        job = await _job(client, cid)
        assert job is not None and job["state"] == "syncing" and job["error"] is None
        generic = (await client.get("/api/v1/admin/connections")).json()
        assert next(c for c in generic if c["connectionId"] == cid)["syncJob"] == job
        again = await _sync(client, cid)
        assert again.status_code == 409
        assert again.json()["message"] == sync_job.ALREADY_RUNNING
        assert plan.calls == 0

    # The process is gone: the very same row now reads interrupted, and a start is admitted.
    assert (await _job(client, cid))["state"] == "failed"
    assert (await _sync(client, cid)).status_code == 200


async def test_a_job_in_this_process_past_the_grace_still_reads_syncing(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    monkeypatch.setattr(get_settings(), "knowledge_sync_job_start_grace_seconds", 0)
    gate = _hold_snapshots(plan)

    assert (await _sync(client, cid)).status_code == 202
    assert (await _job(client, cid))["state"] == "syncing"
    assert (await _sync(client, cid)).status_code == 409
    await gate.release()
    await _finish(cid)
    assert (await _job(client, cid))["state"] == "synced"


@pytest.mark.parametrize(
    "stamp_of",
    [
        pytest.param(
            lambda: sync_job.syncing_stamp(
                started_at=datetime.now(UTC) - timedelta(seconds=61), trigger="manual"
            ),
            id="past-the-grace",
        ),
        pytest.param(lambda: {"state": "syncing", "trigger": "manual"}, id="no-started-at"),
    ],
)
async def test_an_orphan_reads_interrupted_and_admits_a_start_at_once(api, stamp_of):
    client, _plan = api
    admin, cid = await _ready(client)
    stamp = stamp_of()
    await _seed_job(admin, cid, stamp)

    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "failed"
    assert job["error"] == "The sync was interrupted before it finished — try again."
    assert job["result"] is None and job["finishedAt"] is None

    # Read-time only: the stored record keeps its evidence.
    assert await _raw_job(admin, cid) == stamp

    r = await _sync(client, cid)
    assert r.status_code == 200, r.text
    assert (await _job(client, cid))["state"] == "synced"


async def test_two_simultaneous_starts_are_one_job_and_one_409(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    gate = _hold_snapshots(plan)

    first, second = await asyncio.gather(_sync(client, cid), _sync(client, cid))

    assert sorted([first.status_code, second.status_code]) == [202, 409]
    refused = first if first.status_code == 409 else second
    assert refused.json()["message"] == sync_job.ALREADY_RUNNING
    await gate.release()
    await _finish(cid)
    assert plan.calls == 1


async def test_a_held_row_lock_is_a_409_not_a_hang(api, monkeypatch):
    client, plan = api
    admin, cid = await _ready(client)
    monkeypatch.setattr(sync_job, "_LOCK_TIMEOUT", "100ms")

    async with tenant_session(admin) as holder:
        await holder.execute(
            select(InstanceConnection)
            .where(InstanceConnection.connection_id == uuid.UUID(cid))
            .with_for_update()
        )
        r = await asyncio.wait_for(_sync(client, cid), timeout=10)

    assert r.status_code == 409
    assert r.json()["message"] == sync_job.ALREADY_RUNNING
    assert plan.calls == 0
    assert await _raw_job(admin, cid) is None


async def test_a_revoked_row_is_the_same_409_and_its_job_is_untouched(api):
    client, plan = api
    admin, cid = await _ready(client)
    kept = sync_job.terminal_stamp(
        sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual"),
        state="failed",
        error="old",
    )
    await _seed_job(admin, cid, kept, status="revoked")

    r = await _sync(client, cid)
    assert r.status_code == 409
    assert r.json()["message"] == "Connection is revoked; reconnect before syncing."
    assert plan.calls == 0
    assert await _raw_job(admin, cid) == kept


async def test_no_luciel_is_the_same_404_and_its_job_is_untouched(api):
    client, plan = api
    admin = await _signup_verify_login(client)  # an account with no Luciel yet
    cid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=uuid.uuid4(),
                connection_type="knowledge_source",
                auth_class="provisioned_resource",
                provider="website_crawl",
                status="connected",
            )
        )

    r = await _sync(client, str(cid))
    assert r.status_code == 404
    assert r.json()["message"] == "No Luciel."
    assert plan.calls == 0
    assert await _raw_job(admin, str(cid)) is None


class _DownEmbedder:
    dim = 1536
    model_name = "text-embedding-3-small"

    async def embed(self, texts):  # noqa: ANN001, ANN201
        raise EmbeddingError("openai_error:RateLimitError")


async def test_a_down_embedder_is_the_shared_503_inline_and_failed_deferred(api, monkeypatch):
    client, plan = api
    _admin, cid = await _ready(client)
    monkeypatch.setattr(service, "get_embedder", lambda *a, **k: _DownEmbedder())

    inline = await _sync(client, cid)
    assert inline.status_code == 503
    assert inline.json() == {"message": EMBEDDING_DOWN, "retryAfterSeconds": 300}
    # One sentence: the paste route's handler serves the very same words.
    paste = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "Walk-ins", "text": "We welcome walk-ins on weekdays."},
    )
    assert paste.status_code == 503 and paste.json() == inline.json()
    job = await _job(client, cid)
    assert job is not None and job["state"] == "failed" and job["error"] == EMBEDDING_DOWN

    _inline_wait(monkeypatch, 0.05)
    gate = _hold_snapshots(plan)
    assert (await _sync(client, cid)).status_code == 202
    await gate.release()
    await _finish(cid)
    job = await _job(client, cid)
    assert job is not None and job["state"] == "failed" and job["error"] == EMBEDDING_DOWN
    assert job["finishedAt"]


async def test_the_row_keeps_the_whole_pass_and_the_wire_serves_four_lists_and_halted(
    api, monkeypatch
):
    class _BudgetSkipped:
        def extract(self, text: str) -> ExtractionResult:
            return ExtractionResult(entities=[], edges=[])

        async def aextract(self, text: str, *, admin_id=None) -> ExtractionResult:  # noqa: ANN001
            return ExtractionResult(entities=[], edges=[], llm_skipped=True)

    client, plan = api
    admin, cid = await _ready(client)
    plan.pages = ["/", "/a", "/b", "/c"]
    assert (await _sync(client, cid)).status_code == 200

    monkeypatch.setattr(sync, "get_extractor", lambda: _BudgetSkipped())
    plan.pages = ["/", "/a", "/new"]
    plan.revision = "r2"
    plan.failed_ids = [f"{SITE}/b"]
    plan.unreached_ids = [f"{SITE}/c"]
    assert (await _sync(client, cid)).status_code == 200

    raw = await _raw_job(admin, cid)
    assert raw is not None and raw["state"] == "synced"
    result = raw["result"]
    assert result["failed"] == [f"{SITE}/b"]
    assert result["unreached"] == [f"{SITE}/c"]
    assert sorted(result["graph_deferred"]) == [f"{SITE}/", f"{SITE}/a", f"{SITE}/new"]
    assert result["halted"] is None
    assert result["added"] == [f"{SITE}/new"]
    assert set(result) == {
        "added",
        "updated",
        "removed",
        "refused",
        "failed",
        "unreached",
        "graph_deferred",
        "halted",
    }
    served = await _job(client, cid)
    assert served is not None
    assert set(served["result"]) == {"added", "updated", "removed", "refused", "halted"}
    assert served["result"]["halted"] is None


async def test_data_stamp_and_lock_release_become_visible_together(api, monkeypatch):
    client, plan = api
    admin, cid = await _ready(client)
    seen: list[tuple[str, int]] = []
    real = sync_job._conditional_stamp

    async def _watching(session, admin_id, connection_id, value, own):  # noqa: ANN001, ANN202
        landed = await real(session, admin_id, connection_id, value, own)
        # The stamp is written, not committed: another session still reads the job as
        # running, with no pages, and its lock held.
        raw = await _raw_job(admin, cid)
        seen.append((raw["state"], len(await _sources_of(admin, cid))))
        async with tenant_session(admin) as other:
            assert sync.sync_lock_k2(connection_id) in await sync.live_sync_lock_keys(other)
        return landed

    monkeypatch.setattr(sync_job, "_conditional_stamp", _watching)
    assert (await _sync(client, cid)).status_code == 200

    assert seen == [("syncing", 0)]
    assert (await _raw_job(admin, cid))["state"] == "synced"
    assert len(await _sources_of(admin, cid)) == 1


async def test_a_failed_stamp_keeps_the_data_and_is_retried(api, monkeypatch, caplog):
    client, plan = api
    admin, cid = await _ready(client)
    monkeypatch.setattr(sync_job, "_STAMP_RETRY_FIRST_BACKOFF", 0.01)
    real = sync_job._conditional_stamp
    attempts: list[int] = []

    async def _failing_twice(session, admin_id, connection_id, value, own):  # noqa: ANN001, ANN202
        attempts.append(1)
        if len(attempts) <= 2:
            raise RuntimeError("the stamp failed")
        return await real(session, admin_id, connection_id, value, own)

    monkeypatch.setattr(sync_job, "_conditional_stamp", _failing_twice)
    r = await _sync(client, cid)

    assert r.status_code == 200, r.text  # the sync did commit
    assert len(attempts) == 3
    assert len(await _sources_of(admin, cid)) == 1
    assert (await _raw_job(admin, cid))["state"] == "synced"


async def test_a_stamp_that_never_lands_is_an_error_line_and_reads_interrupted(
    api, monkeypatch, caplog
):
    client, plan = api
    admin, cid = await _ready(client)
    monkeypatch.setattr(sync_job, "_STAMP_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(sync_job, "_STAMP_RETRY_FIRST_BACKOFF", 0.01)
    monkeypatch.setattr(get_settings(), "knowledge_sync_job_start_grace_seconds", 0)

    async def _refused(session, admin_id, connection_id, value, own):  # noqa: ANN001, ANN202
        raise RuntimeError("the stamp failed")

    monkeypatch.setattr(sync_job, "_conditional_stamp", _refused)
    with caplog.at_level(logging.ERROR, logger="app.knowledge.sync_job"):
        assert (await _sync(client, cid)).status_code == 200

    assert "knowledge_sync_job_stamp_lost" in [r.getMessage() for r in caplog.records]
    assert (await _job(client, cid))["state"] == "failed"


@pytest.mark.parametrize("replacement", ["cleared", "superseded"])
async def test_a_terminal_stamp_never_overwrites_a_record_it_does_not_own(
    api, monkeypatch, replacement: str
):
    client, plan = api
    admin, cid = await _ready(client)
    newer = (
        None
        if replacement == "cleared"
        else sync_job.syncing_stamp(
            started_at=datetime.now(UTC) + timedelta(seconds=30), trigger="scheduled"
        )
    )

    async def _replace() -> None:
        await _seed_job(admin, cid, newer)

    plan.during_snapshot = _replace
    assert (await _sync(client, cid)).status_code == 200
    assert await _raw_job(admin, cid) == newer


async def test_a_page_the_model_cannot_answer_is_updated_once_not_on_every_pass(api, monkeypatch):
    """An unparseable graph answer used to leave the revision unbumped: a re-embed and an
    LLM call on every pass, and "updated" in the owner's crawl notice with nothing changed."""

    class _Prose:
        calls = 0

        async def complete(self, *a: object, **k: object) -> object:
            type(self).calls += 1

            class _Answer:
                text = "I could not find any entities in this text."
                truncated = False

            return _Answer()

    client, plan = api
    _admin, cid = await _ready(client)
    extractor = LlmGraphExtractor(router_factory=_Prose)
    monkeypatch.setattr(sync, "get_extractor", lambda: extractor)
    plan.text = "We are open nine to five on weekdays, and on Saturdays by appointment only."

    first = await _sync(client, cid)
    assert first.status_code == 200 and first.json()["added"] == [f"{SITE}/"]
    second = await _sync(client, cid)
    assert second.status_code == 200, second.text
    assert second.json() == {"added": [], "updated": [], "removed": [], "refused": []}
    assert _Prose.calls == 1  # no second LLM call for the same revision
    plan.revision = "r2"
    third = await _sync(client, cid)
    assert third.json()["updated"] == [f"{SITE}/"]  # a real change still re-ingests


async def test_evidence_after_writes_does_not_deadlock_on_the_jobs_own_locks(api, monkeypatch):
    """A failure AFTER the sync wrote rows: the evidence writer's own transaction updates
    those same rows. Rolling back to the savepoint released them, so it does not wait."""

    class _FailsOnSecondPage:
        calls = 0

        def extract(self, text: str) -> ExtractionResult:
            return ExtractionResult(entities=[], edges=[])

        async def aextract(self, text: str, *, admin_id=None) -> ExtractionResult:  # noqa: ANN001
            type(self).calls += 1
            if type(self).calls == 2:
                raise CrawlUnreachable("the site went away mid-crawl")
            return ExtractionResult(entities=[], edges=[])

    client, plan = api
    admin, cid = await _ready(client)
    plan.pages = ["/", "/a"]
    assert (await _sync(client, cid)).status_code == 200
    monkeypatch.setattr(sync, "get_extractor", lambda: _FailsOnSecondPage())
    plan.revision = "r2"

    r = await asyncio.wait_for(_sync(client, cid), timeout=15)

    assert r.status_code == 409
    assert r.json()["message"] == "Sync unavailable: the site went away mid-crawl"
    assert {src.sync_status for src in await _sources_of(admin, cid)} == {"error"}
    assert (await _job(client, cid))["state"] == "failed"


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
    gate = _hold_snapshots(plan)
    deferred = await _sync(client, cid)
    assert deferred.status_code == 202
    await gate.release()
    await _finish(cid)
    job = await _job(client, cid)
    assert job is not None
    assert job["state"] == "shrink_pending"
    assert job["shrink"] == {"remote": 1, "existing": 4}
    assert job["error"] == message
    assert job["result"] is None

    _inline_wait(monkeypatch, 20.0)
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
    gate = _hold_snapshots(plan)
    deferred = await _sync(client, cid)
    assert deferred.status_code == 202
    await gate.release()
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
    gate = _hold_snapshots(plan)
    assert (await _sync(client, cid)).status_code == 202
    await gate.release()
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


async def test_the_list_reads_pg_locks_once_and_only_when_a_row_says_syncing(api, monkeypatch):
    client, _plan = api
    admin, cid = await _ready(client)
    other = (
        await client.post(
            "/api/v1/admin/knowledge/sync-connections",
            json={"provider": "website_crawl", "crawlUrls": [f"{SITE}/other"]},
        )
    ).json()["connectionId"]
    real = sync.live_sync_lock_keys
    reads: list[int] = []

    async def _counting(session):  # noqa: ANN001, ANN202
        reads.append(1)
        return await real(session)

    monkeypatch.setattr(sync, "live_sync_lock_keys", _counting)
    assert (await client.get("/api/v1/admin/knowledge/sync-connections")).status_code == 200
    assert (await client.get("/api/v1/admin/connections")).status_code == 200
    assert reads == []  # nothing says syncing: no read at all

    stamp = sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual")
    await _seed_job(admin, cid, stamp)
    await _seed_job(admin, other, stamp)
    assert (await client.get("/api/v1/admin/knowledge/sync-connections")).status_code == 200
    assert reads == [1]
    assert (await client.get("/api/v1/admin/connections")).status_code == 200
    assert reads == [1, 1]


# --- the log line and the metric ----------------------------------------------------- #
async def test_one_job_is_one_metric_line_with_its_outcome_dimension(api, capsys):
    client, _plan = api
    _admin, cid = await _ready(client)
    capsys.readouterr()

    assert (await _sync(client, cid)).status_code == 200

    docs = []
    for line in capsys.readouterr().out.splitlines():
        start = line.find('{"_aws"')
        if start >= 0:
            docs.append(json.loads(line[start:]))
    mine = [d for d in docs if "KnowledgeSyncJob" in d]
    assert len(mine) == 1
    doc = mine[0]
    assert doc["Outcome"] == "synced"
    directive = doc["_aws"]["CloudWatchMetrics"][0]
    assert directive["Metrics"][0]["Name"] == "KnowledgeSyncJob"
    assert directive["Dimensions"] == [["Environment"], ["Environment", "Plane", "Outcome"]]


async def test_one_job_is_one_info_line_with_no_url_or_content(api, caplog):
    client, _plan = api
    _admin, cid = await _ready(client)

    with caplog.at_level(logging.INFO, logger="app.knowledge.sync_job"):
        assert (await _sync(client, cid)).status_code == 200

    lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("knowledge_sync_job ")
    ]
    assert len(lines) == 1
    assert "outcome=synced" in lines[0] and "trigger=manual" in lines[0]
    assert "added=1" in lines[0] and "duration_ms=" in lines[0]
    assert "acme.example" not in lines[0] and "open nine to five" not in lines[0]


# --- shutdown -------------------------------------------------------------------------- #
async def test_leaving_the_lifespan_stamps_a_running_job_first(api, monkeypatch):
    import app.main as main

    client, plan = api
    admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    monkeypatch.setattr(sync_job, "DRAIN_WAIT_SECONDS", 0.05)
    plan.delay = 30.0
    order: list[object] = []

    async def _background() -> None:
        order.append((await _raw_job(admin, cid))["state"])

    monkeypatch.setattr(main, "drain_background_work", _background)

    async with main._lifespan(main.app):
        assert (await _sync(client, cid)).status_code == 202

    assert uuid.UUID(cid) not in sync_job._RUNNING
    assert order == ["failed"]  # the stamp was on the row before anything else drained
    raw = await _raw_job(admin, cid)
    assert raw["state"] == "failed"
    assert raw["error"] == "The sync was interrupted by a restart — try again."
    assert raw["finished_at"]


# --- removing a source stops its sync ---------------------------------------------------- #
async def test_disconnect_mid_job_keeps_nothing_and_a_later_start_is_admitted(api, monkeypatch):
    client, plan = api
    admin, cid = await _ready(client)
    _inline_wait(monkeypatch, 0.05)
    gate = _hold_snapshots(plan)

    assert (await _sync(client, cid)).status_code == 202
    await gate.reached()  # the job is in its snapshot, its transaction and its lock held
    # It answers while the job is still running: the job holds no row lock on the connection.
    gone = await asyncio.wait_for(_disconnect(client, cid), timeout=5)
    assert gone.status_code == 200, gone.text
    assert await _job(client, cid) is None
    await gate.release()  # only now may the snapshot return: the disconnect landed mid-job
    await _finish(cid)

    assert await _sources_of(admin, cid) == []
    assert await _job(client, cid) is None
    assert await _raw_job(admin, cid) is None
    generic = (await client.get("/api/v1/admin/connections")).json()
    assert next(c for c in generic if c["connectionId"] == cid)["syncJob"] is None

    refused = await _sync(client, cid)
    assert refused.status_code == 409
    assert refused.json()["message"] == "This source was removed — connect it again to sync it."

    again = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": [f"{SITE}/"]},
    )
    assert again.status_code == 200 and again.json()["connectionId"] == cid
    _inline_wait(monkeypatch, 20.0)
    assert (await _sync(client, cid)).status_code == 200
    assert len(await _sources_of(admin, cid)) == 1


async def test_disconnect_during_the_inline_wait_is_the_409_in_the_owners_words(api):
    client, plan = api
    admin, cid = await _ready(client)

    async def _remove() -> None:
        assert (await _disconnect(client, cid)).status_code == 200

    plan.during_snapshot = _remove
    r = await _sync(client, cid)

    assert r.status_code == 409
    assert r.json()["message"] == (
        "This source was removed while it was syncing — nothing from that sync was kept."
    )
    assert await _sources_of(admin, cid) == []
    assert await _raw_job(admin, cid) is None


async def test_disconnect_between_two_pages_unwinds_the_pages_already_written(api, monkeypatch):
    client, plan = api
    admin, cid = await _ready(client)

    class _RemovesOnFirstPage:
        calls = 0

        def extract(self, text: str) -> ExtractionResult:
            return ExtractionResult(entities=[], edges=[])

        async def aextract(self, text: str, *, admin_id=None) -> ExtractionResult:  # noqa: ANN001
            type(self).calls += 1
            if type(self).calls == 1:
                await _seed_job(admin, cid, None, status="not_connected")
            return ExtractionResult(entities=[], edges=[])

    monkeypatch.setattr(sync, "get_extractor", lambda: _RemovesOnFirstPage())
    plan.pages = ["/", "/a", "/b"]

    r = await _sync(client, cid)

    assert r.status_code == 409
    assert _RemovesOnFirstPage.calls == 1  # the second page was never ingested
    assert await _sources_of(admin, cid) == []


@pytest.mark.parametrize("old_status", ["error", "not_connected"])
async def test_a_crawl_requested_again_clears_the_old_failure(api, old_status: str):
    client, _plan = api
    admin, cid = await _ready(client)
    failed = sync_job.terminal_stamp(
        sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual"),
        state="failed",
        error="Sync unavailable: the provider refused the stored credential — reconnect it.",
    )
    await _seed_job(admin, cid, failed, status=old_status)

    again = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": [f"{SITE}/"]},
    )

    assert again.status_code == 200
    assert again.json()["status"] == "connected" and again.json()["syncJob"] is None
    assert await _raw_job(admin, cid) is None


async def test_a_reconnect_leaves_a_live_jobs_record_alone(api):
    client, _plan = api
    admin, cid = await _ready(client)
    stamp = sync_job.syncing_stamp(
        started_at=datetime.now(UTC) - timedelta(hours=1), trigger="scheduled"
    )
    await _seed_job(admin, cid, stamp, status="error")

    async with _held_lock(admin, cid):
        again = await client.post(
            "/api/v1/admin/knowledge/sync-connections",
            json={"provider": "website_crawl", "crawlUrls": [f"{SITE}/"]},
        )
        assert again.status_code == 200
        assert again.json()["syncJob"]["state"] == "syncing"
        assert await _raw_job(admin, cid) == stamp


async def test_a_reconnect_through_the_oauth_callback_clears_the_old_failure(api, monkeypatch):
    """The review's "Connected" beside "Last sync failed — reconnect it"."""
    import app.api.v1.admin.knowledge as knowledge_api
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    from tests.contract.test_knowledge_scope_api import FakeDriveTransport, _state_from

    configured = Settings(
        google_client_id="gcid",
        google_client_secret="gcsecret",
        google_redirect_uri="https://api.example.com/api/v1/connections/google/callback",
    )
    monkeypatch.setattr(providers_module, "get_settings", lambda: configured)
    monkeypatch.setattr(registry_module, "get_settings", lambda: configured)
    monkeypatch.setattr(knowledge_api, "oauth_transport", lambda: FakeDriveTransport({}))
    client, _plan = api
    admin, _crawl = await _ready(client)
    created = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "google_drive"}
    )
    assert created.status_code == 201, created.text
    cid = created.json()["connectionId"]
    failed = sync_job.terminal_stamp(
        sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual"),
        state="failed",
        error="Sync unavailable: the provider refused the stored credential — reconnect it.",
    )
    await _seed_job(admin, cid, failed, status="error")
    assert (await _job(client, cid))["state"] == "failed"

    back = await client.post(
        f"/api/v1/admin/knowledge/sync-connections/{cid}/oauth-callback",
        json={"code": "auth-code", "state": _state_from(created.json())},
    )

    assert back.status_code == 200, back.text
    assert back.json()["status"] == "connected" and back.json()["syncJob"] is None
    assert await _raw_job(admin, cid) is None


async def test_the_health_sweep_bringing_a_row_back_clears_the_old_failure(api):
    from app.connections import health

    client, _plan = api
    admin, cid = await _ready(client)
    failed = sync_job.terminal_stamp(
        sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual"),
        state="failed",
        error="Sync unavailable: reconnect it.",
    )
    await _seed_job(admin, cid, failed, status="error")

    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == uuid.UUID(cid))
            )
        ).scalar_one()
        await health._transition(
            s, conn, new_status="connected", detail=None, now=datetime.now(UTC), old_status="error"
        )

    assert await _raw_job(admin, cid) is None


# --- the per-source re-sync is the same job -------------------------------------------- #
async def test_resync_source_while_a_job_runs_is_a_409_with_one_provider_call(api, monkeypatch):
    client, plan = api
    admin, cid = await _ready(client)
    assert (await _sync(client, cid)).status_code == 200
    sid = (await _sources_of(admin, cid))[0].id
    calls_before = plan.calls
    _inline_wait(monkeypatch, 0.05)
    gate = _hold_snapshots(plan)

    assert (await _sync(client, cid)).status_code == 202
    r = await client.post(f"/api/v1/admin/knowledge/sources/{sid}/resync")
    assert r.status_code == 409
    assert r.json()["message"] == sync_job.ALREADY_RUNNING
    await gate.release()
    await _finish(cid)
    assert plan.calls == calls_before + 1


async def test_resync_source_deferred_is_a_202_and_inline_is_the_refreshed_source(api, monkeypatch):
    client, plan = api
    admin, cid = await _ready(client)
    assert (await _sync(client, cid)).status_code == 200
    sid = (await _sources_of(admin, cid))[0].id

    inline = await client.post(f"/api/v1/admin/knowledge/sources/{sid}/resync")
    assert inline.status_code == 200, inline.text
    assert inline.json()["sourceId"] == str(sid) and inline.json()["syncStatus"] == "synced"
    assert (await _job(client, cid))["state"] == "synced"  # a stamped record, as the sync route

    _inline_wait(monkeypatch, 0.05)
    gate = _hold_snapshots(plan)
    deferred = await client.post(f"/api/v1/admin/knowledge/sources/{sid}/resync")
    assert deferred.status_code == 202, deferred.text
    body = deferred.json()
    assert body["connectionId"] == cid and body["state"] == "syncing" and body["startedAt"]
    await gate.release()
    await _finish(cid)
    assert (await _job(client, cid))["startedAt"] == body["startedAt"]
    assert (await _job(client, cid))["state"] == "synced"


# --- a lock refusal leaves no false failure on the row ----------------------------------- #
async def test_a_start_the_lock_refuses_answers_409_and_leaves_no_failure_on_the_row(api):
    """The owner removed a crawl and added it back while another run still held the lock
    (the record is NULL, so the start is admitted). That holder's stamp is keyed on its own
    ``started_at`` and could never replace a failure written here, so the row would read
    "Last sync failed: already running" long after nothing ran."""
    client, plan = api
    admin, cid = await _ready(client)

    async with _held_lock(admin, cid):
        r = await _sync(client, cid)
        assert r.status_code == 409
        assert r.json()["message"] == sync_job.ALREADY_RUNNING
        assert await _raw_job(admin, cid) is None
    assert plan.calls == 0
    assert await _job(client, cid) is None
    # Nothing is stuck: the next start runs.
    assert (await _sync(client, cid)).status_code == 200


# --- a removal is reported as a removal, whatever the sync raised ------------------------ #
@pytest.mark.parametrize(
    "error",
    [ProviderUnavailable("No access token on file"), CrawlUnreachable("crawl fetched no pages")],
    ids=["no-credential", "no-page"],
)
async def test_a_removal_that_surfaces_as_a_provider_failure_is_the_removal_409(api, error):
    """A disconnect deletes the secret, so a snapshot that starts after one raises
    ProviderUnavailable before the sync's first status look. That is the removal, not a
    dead credential: no "Sync unavailable" 409, and no D10 clock on the kept sources."""
    client, plan = api
    admin, cid = await _ready(client)
    assert (await _sync(client, cid)).status_code == 200

    async def _remove() -> None:
        assert (await _disconnect(client, cid)).status_code == 200

    plan.during_snapshot = _remove
    plan.error = error
    r = await _sync(client, cid)

    assert r.status_code == 409
    assert r.json()["message"] == sync_job.STOPPED_BY_REMOVAL
    assert await _raw_job(admin, cid) is None
    kept = await _sources_of(admin, cid)
    assert len(kept) == 1
    assert kept[0].paused_since is None
    assert kept[0].sync_status == "synced"


async def test_the_dead_credential_flag_never_pauses_a_removed_row(api):
    client, _plan = api
    admin, cid = await _ready(client)
    await _seed_job(admin, cid, None, status="not_connected")
    async with tenant_session(admin) as s:
        s.add(
            KnowledgeSource(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=(await s.execute(select(Instance.id))).scalar_one(),
                name="Kept",
                origin="website_crawl",
                source_origin="website_crawl",
                external_id="kept",
                connection_id=uuid.UUID(cid),
                ingestion_status="ready",
                sync_status="synced",
            )
        )

    await sync_job.flag_dead_credential(admin, uuid.UUID(cid))

    kept = await _sources_of(admin, cid)
    assert [k.paused_since for k in kept] == [None]
    async with tenant_session(admin) as s:
        status = (
            await s.execute(
                select(InstanceConnection.status).where(
                    InstanceConnection.connection_id == uuid.UUID(cid)
                )
            )
        ).scalar_one()
    assert status == "not_connected"


# --- a run halted by a pause says so ------------------------------------------------------ #
async def test_a_sync_halted_by_a_pause_says_so_on_the_wire(api, capsys):
    client, plan = api
    admin, cid = await _ready(client)
    assert (await _sync(client, cid)).status_code == 200

    async def _pause() -> None:
        async with tenant_session(admin) as s:
            await s.execute(update(Instance).values(state=LucielState.paused))

    plan.during_snapshot = _pause
    plan.pages = ["/", "/changed"]
    capsys.readouterr()
    r = await _sync(client, cid)

    assert r.status_code == 200
    assert r.json() == {
        "added": [],
        "updated": [],
        "removed": [],
        "refused": [],
        "halted": "instance_not_serving",
    }
    job = await _job(client, cid)
    assert job is not None
    assert job["result"]["halted"] == "instance_not_serving"
    outcomes = [
        json.loads(line[line.find('{"_aws"') :])["Outcome"]
        for line in capsys.readouterr().out.splitlines()
        if '{"_aws"' in line and "KnowledgeSyncJob" in line
    ]
    assert outcomes == ["halted"]
